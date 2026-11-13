"""Codex through its official Python SDK (`openai-codex`), which drives a local `codex app-server` over JSON-RPC:
typed items and turn results instead of parsing CLI output, and an interrupt at the time limit instead of killing a
process.

- Sessions run in Codex's workspace-write sandbox in the workspace, with approvals denied. MCP servers run outside
  the sandbox; their tools are pre-approved (with approvals denied, a tool call would otherwise be refused).
- The Stop gate is a Codex command hook (`python -m megawob_harness.checks stop-hook ...`). Codex runs only hooks
  the user has trusted, and keeps that trust (`hooks.state."<key>".trusted_hash`) in the user config, which a
  session override cannot set. So a session with a Stop gate runs in a throwaway CODEX_HOME: the user's
  config.toml, a link to their auth.json, and a trust entry for the harness's own hook, whose hash the app server
  reports in `hooks/list`. Nothing of it lands in the user's Codex home.
- Every thread is ephemeral, so nothing is written to the Codex history.

Sign in once with `codex login` (ChatGPT plan or API key). `CODEX_HOME` selects another Codex home as usual.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from openai_codex import ApprovalMode, Codex, CodexConfig, LocalImageInput, Sandbox, TextInput

from .base import AgentError, Runtime, Session, SessionSpec, Turn

# Codex features a scripted agent never needs (subagents, connectors, its own browser or desktop control).
QUIET = ("agents.enabled=false", "features.apps=false", "features.browser_use=false", "features.computer_use=false")


def mcp_overrides(mcp: dict[str, list[str]]) -> list[str]:
    out = []
    for name, (command, *args) in mcp.items():
        out += [f"mcp_servers.{name}.command={json.dumps(command)}", f"mcp_servers.{name}.args={json.dumps(args)}",
                f"mcp_servers.{name}.tool_timeout_sec=900", f"mcp_servers.{name}.startup_timeout_sec=60",
                f'mcp_servers.{name}.default_tools_approval_mode="approve"']
    return out


def stop_hook_command(spec: SessionSpec) -> str:
    gate = spec.stop_gate
    args = [sys.executable, "-m", "megawob_harness.checks", "stop-hook", "--workspace", str(gate.workspace),
            "--state", str(gate.ledger.dir), "--max-blocks", str(gate.max_blocks)]
    for name in gate.ignore:
        args += ["--ignore", name]
    return shlex.join(args)


def stop_hook_override(command: str) -> str:
    return f"hooks.Stop=[{{hooks=[{{type={json.dumps('command')},command={json.dumps(command)},timeout=120}}]}}]"


def trust_entry(key: str, current_hash: str) -> str:
    return f"\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(current_hash)}\n"


def _user_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _hooks(codex: Codex, cwd: str) -> list:
    from openai_codex.generated.v2_all import HooksListResponse
    listing = codex._client.request("hooks/list", {"cwds": [cwd]}, response_model=HooksListResponse)
    return [getattr(h, "root", h) for entry in listing.data for h in entry.hooks]


class CodexRuntime(Runtime):
    name = "codex"

    def _config(self) -> dict:
        return {"model_reasoning_effort": self.effort} if self.effort else {}

    def _codex(self, overrides=(), home: Path | None = None) -> Codex:
        env = dict(os.environ)
        if home:
            env["CODEX_HOME"] = str(home)
        return Codex(CodexConfig(config_overrides=(*QUIET, *overrides), env=env))

    def session(self, spec: SessionSpec) -> CodexSession:
        return CodexSession(self, spec)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        with self._codex() as codex, tempfile.TemporaryDirectory(prefix="harness-") as cwd:
            thread = codex.thread_start(model=self.model, sandbox=Sandbox.read_only, cwd=cwd, ephemeral=True,
                                        approval_mode=ApprovalMode.deny_all, config=self._config())
            return _run(thread, _inputs(prompt, images), seconds)

    def image(self, prompt: str, out: Path, refs=(), seconds: float = 600) -> dict:
        """Codex's built-in image tool draws (or, with reference images, edits) one picture into `out` (PNG)."""
        refs = [Path(p) for p in refs]
        how = ("Pass the attached image" + ("s" if len(refs) > 1 else "") + " as reference images, so the result "
               "is an edit of " + ("them" if len(refs) > 1 else "it") + "." if refs else "Use no reference images.")
        message = ("Call the image generation tool exactly once, with the text between <<<PROMPT and PROMPT>>> "
                   f"verbatim as its prompt and no transparent background. {how} Do not run commands, do not "
                   "describe the result and do not call the tool again. Then reply DONE.\n<<<PROMPT\n"
                   + prompt.strip() + "\nPROMPT>>>")
        with self._codex() as codex, tempfile.TemporaryDirectory(prefix="harness-") as cwd:
            thread = codex.thread_start(model=self.model, sandbox=Sandbox.read_only, cwd=cwd, ephemeral=True,
                                        approval_mode=ApprovalMode.deny_all, config=self._config())
            turn, items = _run(thread, _inputs(message, refs), seconds, keep_items=True)
        made = [i for i in items if getattr(i, "type", "") == "imageGeneration" and i.result]
        if not made:
            raise AgentError(f"no image ({turn.status}): {turn.text[:300]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(made[-1].result))
        if made[-1].saved_path:  # Codex keeps its own copy under $CODEX_HOME/generated_images
            Path(str(made[-1].saved_path)).unlink(missing_ok=True)
        return {"seconds": turn.seconds, "usage": turn.usage, "revised_prompt": made[-1].revised_prompt}


class CodexSession(Session):
    def __init__(self, runtime: CodexRuntime, spec: SessionSpec):
        self.runtime, self.spec = runtime, spec
        self._codex = self._thread = None
        self._home: Path | None = None

    def __enter__(self) -> CodexSession:
        overrides = mcp_overrides(self.spec.mcp)
        if self.spec.stop_gate:
            command = stop_hook_command(self.spec)
            overrides.append(stop_hook_override(command))
            self._home = self._trusted_home(command, overrides)
        self._codex = self.runtime._codex(overrides, self._home).__enter__()
        self._thread = self._codex.thread_start(model=self.runtime.model, sandbox=Sandbox.workspace_write,
                                                cwd=str(self.spec.cwd), ephemeral=True,
                                                approval_mode=ApprovalMode.deny_all, config=self.runtime._config())
        return self

    def _trusted_home(self, command: str, overrides: list[str]) -> Path:
        home = Path(tempfile.mkdtemp(prefix="harness-codex-home-"))
        user = _user_home()
        if (user / "auth.json").exists():
            (home / "auth.json").symlink_to(user / "auth.json")
        config = (user / "config.toml").read_text() if (user / "config.toml").is_file() else ""
        (home / "config.toml").write_text(config)
        with self.runtime._codex(overrides, home) as codex:
            mine = [h for h in _hooks(codex, str(self.spec.cwd)) if getattr(h, "command", None) == command]
        if not mine:
            raise AgentError("Codex did not load the harness's Stop hook (hooks/list does not show it)")
        (home / "config.toml").write_text(config + trust_entry(mine[0].key, mine[0].current_hash))
        return home

    def __exit__(self, *exc) -> None:
        try:
            if self._codex:
                self._codex.__exit__(*exc)
        finally:
            if self._home:
                shutil.rmtree(self._home, ignore_errors=True)

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        return _run(self._thread, _inputs(text, images), seconds, model)


def _inputs(text: str, images=()) -> list:
    return [TextInput(text), *(LocalImageInput(str(Path(p).resolve())) for p in images)]


def _run(thread, inputs: list, seconds: float, model: str | None = None, keep_items: bool = False):
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
    return (turn, items) if keep_items else turn
