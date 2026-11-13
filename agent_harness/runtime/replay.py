"""A scripted runtime: the agent's moves are Python functions, so the pipeline, the gate and the Stop hook can be
tested without a model or an API key.

Each `turn` (and each continuation the Stop gate forces) takes the next step from `steps`. A step gets a
`ReplayContext` (the workspace, the prompt, the tools) and returns the turn's final text. The tools are the same
functions the MCP server would serve (`agent_harness.tools.build`), called in-process.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .base import AgentError, Runtime, Session, SessionSpec, Turn


@dataclass
class ReplayContext:
    cwd: Path
    prompt: str
    model: str | None
    tools: dict[str, Callable]
    calls: Counter = field(default_factory=Counter)

    def call(self, name: str, **kwargs):
        self.calls[name] += 1
        return self.tools[name](**kwargs)


Step = Callable[[ReplayContext], str]


class ReplayRuntime(Runtime):
    name = "replay"

    def __init__(self, steps: list[Step] = (), answers: list[dict] = (), model: str | None = "replay"):
        super().__init__(model)
        self.steps, self.answers = list(steps), list(answers)
        self.prompts: list[str] = []

    def next_step(self) -> Step:
        if not self.steps:
            raise AgentError("the replay script has no step left")
        return self.steps.pop(0)

    def session(self, spec: SessionSpec) -> ReplaySession:
        return ReplaySession(self, spec)

    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        self.prompts.append(prompt)
        if not self.answers:
            raise AgentError("the replay script has no answer left")
        return Turn("completed", json.dumps(self.answers.pop(0)), 0.0)


class ReplaySession(Session):
    def __init__(self, runtime: ReplayRuntime, spec: SessionSpec):
        self.runtime, self.spec = runtime, spec
        self.model = runtime.model
        self._tools: dict[str, Callable] | None = None

    def tools(self) -> dict[str, Callable]:
        if self._tools is None:
            from ..tools import build
            self._tools = {}
            for command in self.spec.mcp.values():  # [python, -m, agent_harness.tools, spec.json]
                self._tools.update(build(json.loads(Path(command[-1]).read_text())))
        return self._tools

    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn:
        self.model = model or self.model
        ctx = ReplayContext(self.spec.cwd, text, self.model, self.tools())
        self.runtime.prompts.append(text)
        final = self.runtime.next_step()(ctx)
        while self.spec.stop_gate and (reason := self.spec.stop_gate.decide()):
            ctx.prompt = reason  # the Stop hook's reason is the agent's next instruction, in the same turn
            self.runtime.prompts.append(reason)
            final = self.runtime.next_step()(ctx)
        return Turn("completed", final, 0.0, {}, dict(ctx.calls))
