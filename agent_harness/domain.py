"""What the harness needs to know about one kind of work.

A domain says how to set up a workspace for a job, what the agent is told, which tools it gets, which checks decide
whether the work is done and how an independent reviewer scores it. The harness supplies the rest: the agent
runtime, the `check` tool and Stop gate, the fix rounds, the review-gated polish and the run record.

A domain is rebuilt in the tool server's process from its import path and its `config`, so `config` and every job
must be JSON-serializable. A job is a dict with at least an `id`.
"""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from .runtime.base import Runtime

REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["done", "summary", "remaining", "check_token"],
    "properties": {
        "done": {"type": "boolean", "description": "true only when the work is finished and check() passed"},
        "summary": {"type": "string"},
        "remaining": {"type": "array", "items": {"type": "string"}, "description": "what still differs or is open"},
        "check_token": {"type": ["string", "null"], "description": "the token of your last passing check()"},
    },
}

REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["fidelity", "differences"],
    "properties": {"fidelity": {"type": "integer", "minimum": 1, "maximum": 5},
                   "differences": {"type": "array", "items": {"type": "string"}}},
}

FIX_PROMPT = """The final gate failed. These checks must pass:
{fails}

Reproduce each failure before you edit, fix what it shows, then call check() until it passes. Keep the work you have
done. Reply with the same JSON report as before."""

POLISH_PROMPT = """This work was done by another agent and passes every check. Your job is to raise its quality. An
independent review gave it {fidelity} of 5 and lists what still differs:
{differences}
Close as many of these as you can without breaking what works: check() must still pass before you finish. The brief
the work was done from follows."""


class Domain(ABC):
    name = "domain"
    #: files or directories (any depth) that are not part of the work: left out of the check's hash
    ignore: tuple[str, ...] = ()
    report_schema: dict = REPORT_SCHEMA
    fix_prompt: str = FIX_PROMPT
    polish_prompt: str = POLISH_PROMPT

    def __init__(self, **config):
        self.config = config

    @abstractmethod
    def workspace(self, job: dict) -> Path:
        """The folder the agent works in (the only place it may write)."""

    @abstractmethod
    def prepare(self, job: dict) -> None:
        """Create the workspace for `job` (it does not exist yet)."""

    @abstractmethod
    def brief(self, job: dict, minutes: int) -> str:
        """The agent's first instruction."""

    def images(self, job: dict) -> list[Path]:
        """Images attached to the first instruction (a design to match, a screenshot of a bug, ...)."""
        return []

    def tools(self, job: dict) -> list[Callable]:
        """The domain's tools besides `check`: typed, documented functions. They run in the tool server, outside
        the agent's sandbox. A tool may return text, an `agent_harness.tools.Image`, or a list of both."""
        return []

    @abstractmethod
    def quick_check(self, job: dict) -> list[str]:
        """The checks behind the agent's `check` tool: one line per failure, [] when they pass. Fast enough to run
        many times in a turn."""

    @abstractmethod
    def gate(self, job: dict) -> list[str]:
        """The checks that decide whether the work is accepted (usually `quick_check` and more): one line per
        failure. Runs after every turn, whatever the agent reported."""

    def review(self, job: dict, reviewer: Runtime) -> dict | None:
        """An independent score of the finished work (`REVIEW_SCHEMA`), from a reviewer that did not do it. None
        when the domain has no review."""
        return None

    def polish_note(self, review: dict | None) -> str:
        """What the polisher is told before the brief: the review's score and what still differs."""
        from .jsonio import fill
        return fill(self.polish_prompt, fidelity=review["fidelity"] if review else "?",
                    differences="\n".join(f"- {x}" for x in (review or {}).get("differences", [])) or "- (none listed)")

    def finalize(self, job: dict, record: dict) -> None:
        """Called once the run is over (index the result, move the agent's notes out of the workspace, ...)."""


def domain_ref(domain: Domain) -> str:
    cls = type(domain)
    return f"{cls.__module__}:{cls.__qualname__}"


def load_domain(ref: str, config: dict | None = None) -> Domain:
    """A domain from its `module:Class` path."""
    module, _, name = ref.partition(":")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj(**(config or {}))
