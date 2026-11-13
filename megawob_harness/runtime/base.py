"""The runtime interface: what the pipeline needs from an agent, whichever product runs it.

- `Session`: a multi-turn coding agent in one workspace, with MCP tools. A later turn
  continues the same thread and may switch the model (a cheaper model builds, a stronger one fixes).
- `ask`: one JSON answer from a read-only turn that sees some images (reviews, plans). The reviewer never has tools,
  so it cannot grade work it did itself.
- `image`: one generated image, where the runtime has an image tool.

Every turn has a wall-clock limit; at the limit the turn is interrupted and its result kept.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..jsonio import parse_json, schema_prompt


class AgentError(RuntimeError):
    pass


@dataclass
class Turn:
    status: str  # completed | interrupted (time limit) | failed
    text: str
    seconds: float
    usage: dict = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None


@dataclass
class SessionSpec:
    cwd: Path
    #: MCP servers by name: the command line of a stdio server
    mcp: dict[str, list[str]] = field(default_factory=dict)


class Session(ABC):
    """One agent thread in one workspace; use it as a context manager."""

    def __init__(self, runtime: Runtime, spec: SessionSpec):
        self.runtime, self.spec, self.model = runtime, spec, runtime.model

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        pass

    def turn(self, text: str, images=(), seconds: float = 3600, model: str | None = None) -> Turn:
        """One turn; `model` switches the model from this turn on (the thread keeps its context)."""
        return self._turn(text, [Path(p) for p in images], seconds, model)

    @abstractmethod
    def _turn(self, text: str, images: list[Path], seconds: float, model: str | None) -> Turn: ...


class Runtime(ABC):
    name = "runtime"
    session_class: type[Session]

    def __init__(self, model: str | None = None, effort: str | None = None):
        self.model, self.effort = model, effort

    def session(self, spec: SessionSpec) -> Session:
        return self.session_class(self, spec)

    @abstractmethod
    def _answer(self, prompt: str, images: list[Path], seconds: float) -> Turn:
        """One read-only turn with no tools."""

    def ask(self, prompt: str, schema: dict, images=(), seconds: float = 300, tries: int = 2) -> dict:
        """One JSON object matching `schema`, validated here, with a retry."""
        error = ""
        for _ in range(tries):
            turn = self._answer(schema_prompt(prompt, schema), [Path(p) for p in images], seconds)
            try:
                return parse_json(turn.text, schema)
            except ValueError as exc:
                error = f"{turn.status}: {exc}"
        raise AgentError(f"no valid JSON answer: {error}")

    def image(self, prompt: str, out: Path, refs=(), seconds: float = 600) -> dict:
        raise NotImplementedError(f"the {self.name} runtime has no image tool")

    def __repr__(self) -> str:
        return f"{self.name}:{self.model or 'default'}"


def get_runtime(name: str, model: str | None = None, effort: str | None = None) -> Runtime:
    if name == "codex":
        from .codex import CodexRuntime
        return CodexRuntime(model, effort)
    if name == "claude":
        from .claude import ClaudeRuntime
        return ClaudeRuntime(model, effort)
    raise ValueError(f"unknown runtime {name!r} (codex or claude)")
