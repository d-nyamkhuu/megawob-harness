"""Codex through its official Python SDK (`openai-codex`), which drives a local `codex app-server` over JSON-RPC:
typed items and turn results instead of parsing CLI output, and an interrupt at the time limit instead of killing a
process.

- Sessions run in Codex's workspace-write sandbox in the workspace, with approvals denied. MCP servers run outside
  the sandbox; their tools are pre-approved (with approvals denied, a tool call would otherwise be refused).
- Every Codex process runs in a throwaway CODEX_HOME that holds only a link to the user's login (auth.json): no
  user config, AGENTS.md, plugins or MCP servers, so the agent sees only its brief and the harness's tools (as the
  Claude runtime loads no user settings), and nothing lands in the user's Codex home.
- Every thread is ephemeral, so nothing is written to the Codex history.

Sign in once with `codex login` (ChatGPT plan or API key). `CODEX_HOME` selects another Codex home as usual.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from openai_codex import ApprovalMode, Codex, CodexConfig, LocalImageInput, Sandbox, TextInput

from .base import AgentError, Runtime, Session, Turn

# Codex features a scripted agent never needs (subagents, connectors, its own browser or desktop control).
QUIET = ("agents.enabled=false", "features.apps=false", "features.browser_use=false", "features.computer_use=false")


def mcp_overrides(mcp: dict[str, list[str]]) -> list[str]:
    out = []
    for name, (command, *args) in mcp.items():
        out += [f"mcp_servers.{name}.command={json.dumps(command)}", f"mcp_servers.{name}.args={json.dumps(args)}",
                f"mcp_servers.{name}.tool_timeout_sec=900", f"mcp_servers.{name}.startup_timeout_sec=60",
                f'mcp_servers.{name}.default_tools_approval_mode="approve"']
    return out


def _user_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


@contextlib.contextmanager
def isolated_home():
    """A throwaway CODEX_HOME with only a link to the user's login, removed afterwards."""
    home = Path(tempfile.mkdtemp(prefix="harness-codex-home-"))
    try:
        auth = _user_home() / "auth.json"
        if auth.exists():
            (home / "auth.json").symlink_to(auth)
        (home / "config.toml").write_text("")
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


class CodexSession(Session):
    runtime: CodexRuntime

    def __enter__(self) -> CodexSession:
        with contextlib.ExitStack() as stack:
            home = stack.enter_context(isolated_home())
            codex = stack.enter_context(self.runtime.codex(mcp_overrides(self.spec.mcp), home))
            self._thread = self.runtime.thread(codex, self.spec.cwd, Sandbox.workspace_write)
            self._stack = stack.pop_all()
        return self

    def __exit__(self, *exc) -> None:
        self._stack.__exit__(*exc)

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        return _run(self._thread, _inputs(text, images), seconds, model)[0]


class CodexRuntime(Runtime):
    name = "codex"
    session_class = CodexSession

    def codex(self, overrides, home: Path) -> Codex:
        return Codex(CodexConfig(config_overrides=(*QUIET, *overrides), env={**os.environ, "CODEX_HOME": str(home)}))

    def thread(self, codex: Codex, cwd: Path | str, sandbox: Sandbox):
        """An ephemeral thread (nothing in the Codex history) with approvals denied."""
        config = {"model_reasoning_effort": self.effort} if self.effort else {}
        return codex.thread_start(model=self.model, sandbox=sandbox, cwd=str(cwd), ephemeral=True,
                                  approval_mode=ApprovalMode.deny_all, config=config)

    def _once(self, text: str, images, seconds: float) -> tuple[Turn, list]:
        """One read-only turn in a fresh thread, in an empty folder."""
        with (isolated_home() as home, self.codex((), home) as codex,
              tempfile.TemporaryDirectory(prefix="harness-") as cwd):
            return _run(self.thread(codex, cwd, Sandbox.read_only), _inputs(text, images), seconds)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        return self._once(prompt, images, seconds)[0]

    def image(self, prompt: str, out: Path, refs=(), seconds: float = 600) -> dict:
        """Codex's built-in image tool draws (or, with reference images, edits) one picture into `out` (PNG)."""
        refs = [Path(p) for p in refs]
        how = ("Pass the attached image" + ("s" if len(refs) > 1 else "") + " as reference images, so the result "
               "is an edit of " + ("them" if len(refs) > 1 else "it") + "." if refs else "Use no reference images.")
        message = ("Call the image generation tool exactly once, with the text between <<<PROMPT and PROMPT>>> "
                   f"verbatim as its prompt and no transparent background. {how} Do not run commands, do not "
                   "describe the result and do not call the tool again. Then reply DONE.\n<<<PROMPT\n"
                   + prompt.strip() + "\nPROMPT>>>")
        turn, items = self._once(message, refs, seconds)
        made = [i for i in items if getattr(i, "type", "") == "imageGeneration" and i.result]
        if not made:
            raise AgentError(f"no image ({turn.status}): {turn.text[:300]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(made[-1].result))  # Codex's own copy went with the throwaway home
        return {"seconds": turn.seconds, "usage": turn.usage, "revised_prompt": made[-1].revised_prompt}


def _inputs(text: str, images=()) -> list:
    return [TextInput(text), *(LocalImageInput(str(Path(p).resolve())) for p in images)]


def _run(thread, inputs: list, seconds: float, model: str | None = None) -> tuple[Turn, list]:
    """One turn on `thread`, interrupted after `seconds`: the turn and its items."""
    t0 = time.monotonic()
    handle = thread.turn(inputs, model=model)
    timer = threading.Timer(seconds, handle.interrupt)
    timer.start()
    try:
        result = handle.run()
    finally:
        timer.cancel()
    status = getattr(result.status, "value", str(result.status))
    if result.error and status == "failed":
        raise AgentError(f"turn failed: {result.error}")
    items = [getattr(i, "root", i) for i in result.items]
    calls = Counter(getattr(i, "tool", None) for i in items if getattr(i, "type", "") == "mcpToolCall")
    turn = Turn(status, result.final_response or "", round(time.monotonic() - t0, 1),
                result.usage.model_dump(mode="json") if result.usage else {}, dict(calls))
    return turn, items
