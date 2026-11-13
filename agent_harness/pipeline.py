"""One job, start to finish: build, gate and fix, review, polish. No agent's own verdict accepts anything.

1. **Build.** The builder runtime works in the domain's workspace from its brief, with the domain's tools and
   `check`, up to `minutes`. With the Stop gate on, it cannot end its turn before a check has passed on its current
   files (at most `max_stop_blocks` times per turn; the external gate still runs).
2. **Gate and fix.** The domain's gate decides. A failure, a report that is not `done`, or a report without a
   current check token goes back to the same thread as a fix round, on `fix_model` (the thread keeps its context).
   At most `fix_rounds` rounds.
3. **Review.** A separate read-only turn on the reviewer runtime, which did not do the work, scores it 1-5 and lists
   what still differs.
4. **Polish.** The polisher runtime, in a fresh thread, works from the review's list. Then the gate (one fix round)
   and the review run again. The polish is kept only if the gate passes and the score did not drop; otherwise the
   workspace is restored. Skipped when the review gives 5.

The run record (every turn's model, status, time, tokens, cost, tool calls, Stop-gate blocks, proof of check,
report and gate result, both reviews, whether the polish was kept) goes to `<out>/runs/<job id>.json`.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .checks import Ledger, StopGate, tree_hash
from .domain import Domain, domain_ref
from .jsonio import fill, parse_json, write_json
from .runtime.base import AgentError, Runtime, SessionSpec
from .tools import build as build_tools
from .tools import write_spec

NOT_DONE = "your report says done=false (or is missing): finish the remaining work, then report again"
NO_PROOF = ("your report has no check_token from a check() that passed on the files as they are now: call check() "
            "after your last edit, fix what it reports, and put the token of the passing check in your report")


@dataclass
class Settings:
    minutes: int = 60
    fix_rounds: int = 2
    polish: bool = True
    stop_gate: bool = True
    max_stop_blocks: int = 3
    require_proof: bool = True


class Pipeline:
    def __init__(self, domain: Domain, builder: Runtime, *, reviewer: Runtime | None = None,
                 polisher: Runtime | None = None, fix_model: str | None = None, settings: Settings | None = None,
                 out: Path = Path("runs")):
        self.domain, self.builder, self.reviewer, self.polisher = domain, builder, reviewer, polisher
        self.fix_model, self.settings, self.out = fix_model, settings or Settings(), Path(out)

    def run(self, job: dict, force: bool = False) -> dict:
        d, s = self.domain, self.settings
        ws = d.workspace(job)
        if ws.exists():
            if not force:
                raise FileExistsError(f"{ws} exists; pass force=True to redo the job from scratch")
            shutil.rmtree(ws)
        t0 = time.monotonic()
        d.prepare(job)
        record = {"job": job["id"], "domain": d.name, "started": datetime.now().isoformat(timespec="seconds"),
                  "builder": repr(self.builder), "fixer": self.fix_model or self.builder.model,
                  "reviewer": self.reviewer and repr(self.reviewer), "polisher": self.polisher and repr(self.polisher),
                  "settings": asdict(s), "turns": []}
        brief = d.brief(job, s.minutes)
        passed = self._work(self.builder, job, brief, "build", record, s.fix_rounds)
        before = self._review(job) if passed else None
        record["review"] = before
        if passed and self.polisher and s.polish and (before is None or before["fidelity"] < 5):
            with tempfile.TemporaryDirectory(prefix="harness-keep-") as tmp:
                kept = Path(tmp) / "workspace"
                shutil.copytree(ws, kept, symlinks=True)
                note = d.polish_note(before)
                ok = self._work(self.polisher, job, note + "\n\n" + brief, "polish", record, 1)
                after = self._review(job) if ok else None
                keep = ok and (before is None or after is None or after["fidelity"] >= before["fidelity"])
                if not keep:
                    shutil.rmtree(ws)
                    shutil.copytree(kept, ws, symlinks=True)
                record["polish"] = {"kept": keep, "gate_passed": ok, "review": after}
                if keep and after:
                    record["review"] = after
        record.update(passed=passed, fidelity=(record["review"] or {}).get("fidelity"),
                      minutes=round((time.monotonic() - t0) / 60, 1))
        d.finalize(job, record)
        write_json(self.out / "runs" / f"{job['id']}.json", record)
        return record

    def _review(self, job: dict) -> dict | None:
        if not self.reviewer:
            return None
        try:
            return self.domain.review(job, self.reviewer)
        except AgentError as exc:
            print(f"{job['id']}: review failed: {exc}", file=sys.stderr, flush=True)
            return None

    def _work(self, runtime: Runtime, job: dict, first: str, stage: str, record: dict, fix_rounds: int) -> bool:
        """One agent thread on the job: the first turn, then gate failures back as fix rounds. True if accepted."""
        d, s = self.domain, self.settings
        ws = d.workspace(job).resolve()
        schema = d.report_schema
        with tempfile.TemporaryDirectory(prefix="harness-state-") as state_dir:
            state = Path(state_dir)
            spec = write_spec(state / "tools.json", domain=domain_ref(d), config=d.config, job=job, workspace=ws,
                              state=state, ignore=d.ignore)
            build_tools(json.loads(spec.read_text()))  # fail here, not as an agent without tools
            gate = StopGate(ws, state, d.ignore, s.max_stop_blocks) if s.stop_gate else None
            ledger = Ledger(state)
            mcp = {"harness": [sys.executable, "-m", "agent_harness.tools", str(spec)]}
            with runtime.session(SessionSpec(cwd=ws, mcp=mcp, stop_gate=gate)) as session:
                model, blocks = runtime.model, 0
                turn = session.turn(first, images=d.images(job), seconds=s.minutes * 60)
                for rnd in range(fix_rounds + 1):
                    try:
                        report = parse_json(turn.text, schema)
                    except ValueError as exc:
                        report = {"error": str(exc)}
                    fails = d.gate(job)
                    if not report.get("done"):
                        fails.append(NOT_DONE + (f"; {report['error']}" if "error" in report else ""))
                    proof = ledger.verify(report.get("check_token"), tree_hash(ws, d.ignore))
                    if s.require_proof and not proof:
                        fails.append(NO_PROOF)
                    now = gate.blocks() if gate else 0
                    record["turns"].append({
                        "stage": stage if rnd == 0 else f"{stage} fix", "model": model, "status": turn.status,
                        "seconds": turn.seconds, "tool_calls": turn.tool_calls, "stop_blocks": now - blocks,
                        "checks": len(ledger.entries()), "proof": proof, "usage": turn.usage,
                        "cost_usd": turn.cost_usd, "report": copy.deepcopy(report), "gate": fails})
                    blocks = now
                    print(f"{job['id']}: {stage} turn {rnd + 1} ({model or 'default model'}) {turn.status} in "
                          f"{turn.seconds:.0f} s, gate {'passed' if not fails else f'{len(fails)} failures'}",
                          flush=True)
                    if not fails:
                        return True
                    if rnd < fix_rounds:
                        model = self.fix_model or runtime.model
                        turn = session.turn(fill(d.fix_prompt, fails="\n".join(f"- {f}" for f in fails)),
                                            seconds=s.minutes * 30, model=model)
        return False
