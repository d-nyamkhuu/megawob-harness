"""Making the agent verify its own work before it may finish.

Three pieces, all keyed on a hash of the workspace's files:

- **The check ledger.** The agent's `check` tool runs the domain's quick check and appends the result to a ledger
  in a state directory outside the workspace. The agent can read the ledger but cannot write it: its sandbox (Codex)
  or its write guard (Claude) limits writes to the workspace. A passing check gets a one-time token.
- **The Stop gate.** A Stop hook (a command hook in Codex, a callback in the Claude Agent SDK) asks the gate whether
  the agent may end its turn. It may only if the ledger's last check passed on the files as they are now. Otherwise
  the hook blocks the stop and its reason becomes the agent's next instruction, inside the same turn. The gate
  blocks at most `max_blocks` times per turn and never after the turn's deadline, so it cannot loop forever.
- **The proof of check.** The agent's report carries the token of its last passing check. The harness accepts it
  only if that check ran on the current files, so a report that claims "done" without verifying is caught even on a
  runtime without hooks.

None of this replaces the harness's own gate, which runs after the turn whatever the agent did. The ledger and the
gate make the agent run the checks itself, so failures are fixed inside the turn instead of costing a fix round.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

ALWAYS_IGNORED = frozenset({".git", "__pycache__", "node_modules", ".DS_Store"})


def tree_hash(root: Path, ignore: Iterable[str] = ()) -> str:
    """A sha256 over the workspace's file paths and contents. `ignore` names files or directories (any depth)."""
    root, skip = Path(root), ALWAYS_IGNORED | set(ignore)
    if not root.is_dir():
        raise FileNotFoundError(f"no workspace at {root.resolve()}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_dir() or skip.intersection(rel.parts):
            continue
        digest.update(str(rel).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


class Ledger:
    """The checks the agent ran in one session, one JSON line each, in a directory it cannot write."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "checks.jsonl"

    def add(self, tree: str, failures: list[str]) -> dict:
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "hash": tree, "passed": not failures,
                 "failures": failures[:20], "token": secrets.token_hex(8) if not failures else None}
        with self.file.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.file.is_file():
            return []
        return [json.loads(line) for line in self.file.read_text().splitlines() if line.strip()]

    def last(self) -> dict | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def verify(self, token: str | None, tree: str) -> bool:
        """True if `token` is from a passing check of the files whose hash is `tree`."""
        return bool(token) and any(e["token"] == token and e["passed"] and e["hash"] == tree for e in self.entries())


def check_tool(run: Callable[[], list[str]], workspace: Path, ledger: Ledger, ignore: Iterable[str] = ()):
    """The agent's `check` tool: run the domain's quick check, record it, and hand out a token if it passed."""
    ignore = tuple(ignore)

    def check() -> str:
        """Run the checks on the files as they are now: the failures (FAIL lines), or "check passed" with a
        check_token. Your report must carry the token of a check that passed after your last edit; editing a
        file afterwards voids it. You cannot finish until a check has passed on your final files."""
        tree = tree_hash(workspace, ignore)
        try:
            failures = list(run())
        except Exception as exc:  # a crashing check is a failure the agent should see, not a protocol error
            failures = [f"the check crashed: {type(exc).__name__}: {exc}"[:400]]
        entry = ledger.add(tree, failures)
        if failures:
            return "\n".join(f"FAIL {f}" for f in failures) + "\nNo token: fix these and call check() again."
        return f"check passed. check_token: {entry['token']}"

    return check


class StopGate:
    """Whether the agent may end its turn: only after a check passed on its current files."""

    def __init__(self, workspace: Path, state_dir: Path, ignore: Iterable[str] = (), max_blocks: int = 3):
        self.workspace, self.ignore, self.max_blocks = Path(workspace), tuple(ignore), max_blocks
        self.ledger = Ledger(state_dir)
        self._blocks = self.ledger.dir / "stop_blocks.json"
        self._turn_file = self.ledger.dir / "turn.json"

    def _log(self) -> list[dict]:
        return json.loads(self._blocks.read_text()) if self._blocks.is_file() else []

    def _turn(self) -> dict:
        return json.loads(self._turn_file.read_text()) if self._turn_file.is_file() else {"turn": 0, "deadline": None}

    def blocks(self) -> int:
        """How many times the gate blocked a stop in this session."""
        return len(self._log())

    def start_turn(self, seconds: float) -> None:
        """A new turn of at most `seconds`: the block limit counts per turn, and the gate never blocks after the
        turn's time limit (the limit wins over the gate)."""
        self._turn_file.write_text(json.dumps({"turn": self._turn()["turn"] + 1, "deadline": time.time() + seconds}))

    def decide(self) -> str | None:
        """None to let the agent stop, else the reason to give it for continuing."""
        turn, log = self._turn(), self._log()
        if sum(1 for b in log if b["turn"] == turn["turn"]) >= self.max_blocks:
            return None
        if turn["deadline"] and time.time() > turn["deadline"]:
            return None
        last = self.ledger.last()
        if last is None:
            reason = "You have not called check() yet."
        elif last["hash"] != tree_hash(self.workspace, self.ignore):
            reason = "You changed files after your last check(), so it no longer counts."
        elif not last["passed"]:
            reason = ("Your last check() failed:\n" + "\n".join(f"- {f}" for f in last["failures"][:5]) +
                      "\nFix these first.")
        else:
            return None
        reason += (" Call check() and fix what it reports until it passes, then end with your final report, "
                   "carrying the check_token of that passing check.")
        self._blocks.write_text(json.dumps([*log, {"turn": turn["turn"], "reason": reason,
                                                   "at": datetime.now().isoformat(timespec="seconds")}]))
        return reason


def main(argv: list[str] | None = None) -> None:
    """The Stop hook as a command (Codex runs it with the hook input on stdin and reads a decision on stdout)."""
    parser = argparse.ArgumentParser(prog="python -m megawob_harness.checks")
    parser.add_argument("command", choices=["stop-hook"])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--max-blocks", type=int, default=3)
    parser.add_argument("--ignore", action="append", default=[])
    args = parser.parse_args(argv)
    sys.stdin.read()  # the hook input; the gate needs only the files and the ledger
    reason = StopGate(Path(args.workspace), Path(args.state), args.ignore, args.max_blocks).decide()
    print(json.dumps({"decision": "block", "reason": reason} if reason else {}))


if __name__ == "__main__":
    main()
