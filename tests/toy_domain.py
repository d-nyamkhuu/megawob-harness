"""A domain small enough to reason about: write the answer to a file. The quick check wants "42"; the gate also
wants the file to end with a newline."""
from __future__ import annotations

from pathlib import Path

from agent_harness.domain import REVIEW_SCHEMA, Domain


class Toy(Domain):
    name = "toy"
    ignore = ("NOTES.md",)

    def workspace(self, job):
        return Path(self.config["root"]) / job["id"]

    def prepare(self, job):
        self.workspace(job).mkdir(parents=True)
        (self.workspace(job) / "answer.txt").write_text("TODO")

    def brief(self, job, minutes):
        return f"Write the answer to answer.txt within {minutes} minutes."

    def tools(self, job):
        root = self.workspace(job)

        def peek() -> str:
            """The current answer."""
            return (root / "answer.txt").read_text()

        return [peek]

    def quick_check(self, job):
        text = (self.workspace(job) / "answer.txt").read_text()
        return [] if text.strip() == "42" else [f"answer.txt says {text.strip()!r}, not 42"]

    def gate(self, job):
        text = (self.workspace(job) / "answer.txt").read_text()
        return self.quick_check(job) or ([] if text.endswith("\n") else ["answer.txt does not end with a newline"])

    def review(self, job, reviewer):
        return reviewer.ask("Score the answer.", REVIEW_SCHEMA)
