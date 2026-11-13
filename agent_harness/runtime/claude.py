"""Claude through the Claude Agent SDK (`claude-agent-sdk`): Claude Code's agent loop and file tools as a library.

- Sessions get the file tools only (Read, Write, Edit, Glob, Grep) plus the MCP tools, auto-approved, and no shell:
  without an OS sandbox a shell could write anywhere. A PreToolUse hook denies any write outside the workspace,
  which is what keeps the check ledger out of the agent's reach (Codex gets the same from its sandbox).
- The Stop gate is a Stop hook callback in this process; a block's reason becomes the agent's next instruction.
- No user or project settings are loaded (no CLAUDE.md, no user hooks, no other MCP servers): a scripted agent
  sees only its brief and its tools.
- The SDK is async; each session runs it on an event loop thread of its own, so the pipeline stays synchronous and
  can run several builds in threads.

Sign in with `claude login` or set ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    ToolUseBlock,
)

from .base import AgentError, Runtime, Session, SessionSpec, Turn

FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
WRITE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"


class _Loop:
    """An asyncio event loop on a thread of its own."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()


def inside(root: Path, path: str) -> bool:
    """Whether `path` (absolute, or relative to `root`) resolves inside `root`."""
    root = root.resolve()
    target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    return target == root or root in target.parents


def write_guard(root: Path):
    async def guard(data, tool_use_id, context):
        tool_input = data.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if path and not inside(root, path):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"writes are limited to {root}"}}
        return {}
    return guard


def stop_hook(gate):
    async def stop(data, tool_use_id, context):
        reason = gate.decide()
        return {"decision": "block", "reason": reason} if reason else {}
    return stop


def _message(text: str, images: list[Path]) -> dict:
    content = []
    for path in images:
        media = mimetypes.guess_type(path.name)[0] or "image/png"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                    "data": base64.b64encode(path.read_bytes()).decode()}})
    content.append({"type": "text", "text": text})
    return {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None,
            "session_id": "default"}


async def _stream(message: dict):
    yield message


async def _turn(client: ClaudeSDKClient, text: str, images: list[Path], seconds: float) -> Turn:
    t0 = time.monotonic()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(seconds, lambda: asyncio.ensure_future(client.interrupt()))
    calls: Counter = Counter()
    result = None
    try:
        await client.query(_stream(_message(text, images)))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock) and block.name.startswith("mcp__"):
                        calls[block.name.split("__", 2)[-1]] += 1
            elif isinstance(msg, ResultMessage):
                result = msg
    finally:
        timer.cancel()
    if result is None:
        raise AgentError("the turn ended without a result")
    reason = result.terminal_reason or ""
    status = "interrupted" if reason.startswith("aborted") else "failed" if result.is_error else "completed"
    if status == "failed":
        raise AgentError(f"turn failed: {result.subtype} {result.errors or result.result}")
    return Turn(status, result.result or "", round(time.monotonic() - t0, 1), result.usage or {}, dict(calls),
                result.total_cost_usd)


class ClaudeRuntime(Runtime):
    name = "claude"

    def _options(self, cwd: Path, **kw) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(cwd=str(cwd), model=self.model, effort=self.effort, setting_sources=[],
                                  strict_mcp_config=True, **kw)

    def session(self, spec: SessionSpec) -> ClaudeSession:
        return ClaudeSession(self, spec)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        loop = _Loop()
        try:
            with tempfile.TemporaryDirectory(prefix="harness-") as cwd:
                async def answer():
                    async with ClaudeSDKClient(self._options(Path(cwd), tools=[], max_turns=1)) as client:
                        return await _turn(client, prompt, images, seconds)
                return loop.run(answer())
        finally:
            loop.close()


class ClaudeSession(Session):
    def __init__(self, runtime: ClaudeRuntime, spec: SessionSpec):
        self.runtime, self.spec = runtime, spec
        self.model = runtime.model
        self._loop = self._client = None

    def __enter__(self) -> ClaudeSession:
        hooks = {"PreToolUse": [HookMatcher(matcher=WRITE_TOOLS, hooks=[write_guard(self.spec.cwd)])]}
        if self.spec.stop_gate:
            hooks["Stop"] = [HookMatcher(hooks=[stop_hook(self.spec.stop_gate)], timeout=600)]
        servers = {name: {"type": "stdio", "command": cmd[0], "args": cmd[1:]} for name, cmd in self.spec.mcp.items()}
        options = self.runtime._options(self.spec.cwd, tools=FILE_TOOLS, mcp_servers=servers, hooks=hooks,
                                        allowed_tools=[*FILE_TOOLS, *(f"mcp__{n}" for n in servers)],
                                        permission_mode="dontAsk")
        self._loop = _Loop()
        self._client = ClaudeSDKClient(options)
        self._loop.run(self._client.connect())
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._client:
                self._loop.run(self._client.disconnect())
        finally:
            if self._loop:
                self._loop.close()

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        async def go():
            if model and model != self.model:
                await self._client.set_model(model)
                self.model = model
            return await _turn(self._client, text, images, seconds)
        return self._loop.run(go())
