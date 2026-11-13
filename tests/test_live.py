"""The pipeline on the real runtimes (a minute or two each, a little cost):

    HARNESS_LIVE=1 pytest tests/test_live.py

The agent gets a scaffolded site (which already passes the checks) and a short brief: call check() and report. The
build must pass in one turn, through the real tools server, sandbox and gate.
"""
import os

import pytest

from megawob_harness import build, get_runtime
from tests.test_build import design  # noqa: F401  (the stand-in design fixture)

pytestmark = [pytest.mark.megawob,
              pytest.mark.skipif(not os.environ.get("HARNESS_LIVE"), reason="set HARNESS_LIVE=1 to run live agents")]

BRIEF = ('Do not edit anything. Call the check tool once, then reply with only this JSON object: {"done": true, '
         '"fidelity": 1, "remaining_differences": [], "deviations": [], "notes": ""}')


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_live_agent_drives_the_tools_and_passes_the_gate(runtime, design, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(build, "brief", lambda job, minutes: BRIEF)
    rec = build.build(design, tmp_path / "out", get_runtime(runtime),
                      settings=build.Settings(seeds=1, minutes=8, fix_rounds=0))
    turn = rec["turns"][0]
    assert rec["passed"] and turn["tool_calls"].get("check", 0) >= 1, turn
