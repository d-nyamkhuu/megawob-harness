"""Live runs of the Stop gate on the real runtimes (a few minutes, a little cost):

    HARNESS_LIVE=1 pytest tests/test_live.py

The agent gets a scaffolded site (which already passes the checks) and is told to report at once, without checking.
The Stop hook must block that stop, and the agent must then call check() inside the same turn, so the build is
accepted without a fix round.
"""
import os

import pytest

from megawob_harness import build, get_runtime
from tests.test_build import design  # noqa: F401  (the stand-in design fixture)

pytestmark = [pytest.mark.megawob,
              pytest.mark.skipif(not os.environ.get("HARNESS_LIVE"), reason="set HARNESS_LIVE=1 to run live agents")]

HASTY = ('Do not edit anything and do not call any tool. Reply now with only this JSON object: {"done": true, '
         '"fidelity": 1, "remaining_differences": [], "deviations": [], "notes": "", "check_token": null}')


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_stop_hook_makes_a_hasty_agent_check(runtime, design, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(build, "prompt", lambda job, minutes: HASTY)
    rec = build.build(design, tmp_path / "out", get_runtime(runtime),
                      settings=build.Settings(seeds=1, minutes=8, fix_rounds=0))
    turn = rec["turns"][0]
    assert turn["stop_blocks"] >= 1, turn
    assert turn["tool_calls"].get("check", 0) >= 1, turn
    assert rec["passed"] and turn["proof"], turn
