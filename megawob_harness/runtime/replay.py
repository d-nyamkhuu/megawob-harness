"""A scripted runtime: the agent's moves are Python functions, so the pipeline and the gate can be tested without a
model or an API key.

Each `turn` takes the next step from `steps`. A step gets a `ReplayContext` (the workspace, the prompt, the tools)
and returns the turn's final text. The tools are the same functions the MCP server would serve
(`megawob_harness.tools.build`), called in-process.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .base import AgentError, Runtime, Session, SessionSpec, Turn


@dataclass
class ReplayContext:
    cwd: Path
    prompt: str
    model: str | None
    tools: dict[str, Callable]
    pool: ThreadPoolExecutor
    calls: Counter = field(default_factory=Counter)

    def call(self, name: str, **kwargs):
        """Call a tool on the session's tool thread (Playwright's sync API stays on one thread, as in the server)."""
        self.calls[name] += 1
        return self.pool.submit(self.tools[name], **kwargs).result()


Step = Callable[[ReplayContext], str]


class ReplaySession(Session):
    runtime: ReplayRuntime

    def __init__(self, runtime: ReplayRuntime, spec: SessionSpec):
        super().__init__(runtime, spec)
        self._tools: dict[str, Callable] | None = None
        self._pool = ThreadPoolExecutor(1)

    def __exit__(self, *exc) -> None:
        self._pool.shutdown()

    def tools(self) -> dict[str, Callable]:
        if self._tools is None:
            from ..tools import build
            self._tools = {}
            for command in self.spec.mcp.values():  # [python, -m, megawob_harness.tools, spec.json]
                self._tools.update(build(json.loads(Path(command[-1]).read_text())))
        return self._tools

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        self.model = model or self.model
        ctx = ReplayContext(self.spec.cwd, text, self.model, self.tools(), self._pool)
        self.runtime.prompts.append(text)
        final = self.runtime.next_step()(ctx)
        return Turn("completed", final, 0.0, {}, dict(ctx.calls))


class ReplayRuntime(Runtime):
    name = "replay"
    session_class = ReplaySession

    def __init__(self, steps: list[Step] = (), answers: list[dict] = (), model: str | None = "replay"):
        super().__init__(model)
        self.steps, self.answers = list(steps), list(answers)
        self.prompts: list[str] = []

    def next_step(self) -> Step:
        if not self.steps:
            raise AgentError("the replay script has no step left")
        return self.steps.pop(0)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        self.prompts.append(prompt)
        if not self.answers:
            raise AgentError("the replay script has no answer left")
        return Turn("completed", json.dumps(self.answers.pop(0)), 0.0)
