"""Claude through the Claude Agent SDK (`claude-agent-sdk`): Claude Code's agent loop and file tools as a library.

- Sessions get the file tools only (Read, Write, Edit, Glob, Grep) plus the MCP tools, and no shell: without an OS
  sandbox a shell could write anywhere. A permission rule allows writes in the workspace only, and in dontAsk mode
  every other write is denied (Codex gets the same from its sandbox).
- No user or project settings are loaded (no CLAUDE.md, no user hooks, no other MCP servers): a scripted agent
  sees only its brief and its tools.
- The SDK is async; each session keeps an event loop of its own (`asyncio.Runner`) in the thread that uses it, so
  the pipeline stays synchronous and can run several builds in threads.

Sign in with `claude login` or set ANTHROPIC_API_KEY.
"""
import asyncio
import base64
import gc
import mimetypes
import tempfile
import time
from collections import Counter
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, ToolUseBlock

from .base import GRACE, AgentError, Runtime, Session, Turn

FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
# Reads and searches anywhere; `Edit(/**)` is every write (Write and Edit alike) under the session's working folder.
ALLOWED = ["Read", "Glob", "Grep", "Edit(/**)"]


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
    """One turn, interrupted after `seconds`; abandoned if it has not stopped GRACE seconds later."""
    try:
        return await asyncio.wait_for(_receive(client, text, images, seconds), seconds + GRACE)
    except TimeoutError:
        raise AgentError(f"the turn did not stop within {GRACE} s of its interrupt") from None


async def _receive(client: ClaudeSDKClient, text: str, images: list[Path], seconds: float) -> Turn:
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


class ClaudeSession(Session):
    runtime: ClaudeRuntime

    def __enter__(self) -> ClaudeSession:
        servers = {name: {"type": "stdio", "command": cmd[0], "args": cmd[1:]} for name, cmd in self.spec.mcp.items()}
        options = self.runtime.options(self.spec.cwd, tools=FILE_TOOLS, mcp_servers=servers,
                                       allowed_tools=[*ALLOWED, *(f"mcp__{n}" for n in servers)],
                                       permission_mode="dontAsk")
        self._runner = asyncio.Runner()
        self._client = ClaudeSDKClient(options)
        try:
            self._runner.run(self._client.connect())
        except BaseException:
            self._runner.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._runner.run(asyncio.wait_for(self._client.disconnect(), 60))
        finally:
            # After a failed session the CLI's transport may still be open; collected while the loop still runs,
            # it closes cleanly instead of raising "Event loop is closed" later.
            self._client = None
            gc.collect()
            self._runner.close()

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        async def go():
            if model and model != self.model:
                await self._client.set_model(model)
                self.model = model
            return await _turn(self._client, text, images, seconds)
        return self._runner.run(go())


class ClaudeRuntime(Runtime):
    name = "claude"
    session_class = ClaudeSession

    def options(self, cwd: Path, **kw) -> ClaudeAgentOptions:
        # The SDK's 1 MB message limit is below one render() screenshot coming back through the stream.
        return ClaudeAgentOptions(cwd=str(cwd), model=self.model, effort=self.effort, setting_sources=[],
                                  strict_mcp_config=True, max_buffer_size=32 * 1024 * 1024, **kw)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        async def answer():
            with tempfile.TemporaryDirectory(prefix="harness-") as cwd:
                async with ClaudeSDKClient(self.options(Path(cwd), tools=[], max_turns=1)) as client:
                    return await _turn(client, prompt, images, seconds)
        return asyncio.run(answer())
