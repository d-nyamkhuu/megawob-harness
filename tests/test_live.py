"""Live runs of the Stop gate on the real runtimes (they cost a little): HARNESS_LIVE=1 pytest tests/test_live.py.

The brief tells the agent to skip checking and report at once. The Stop hook must block that stop, and the agent
must then call check() inside the same turn, so the work is accepted without a fix round.
"""
import os

import pytest

from agent_harness import Pipeline, Settings, get_runtime
from tests.toy_domain import Toy

pytestmark = pytest.mark.skipif(not os.environ.get("HARNESS_LIVE"), reason="set HARNESS_LIVE=1 to run live agents")


class Hasty(Toy):
    def brief(self, job, minutes):
        return ('Write exactly "42" followed by a newline to answer.txt. Then, without calling any other tool, reply '
                'with only this JSON object: {"done": true, "summary": "wrote 42", "remaining": [], '
                '"check_token": null}')


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_stop_hook_makes_a_hasty_agent_check(runtime, tmp_path):
    record = Pipeline(Hasty(root=str(tmp_path / "ws")), get_runtime(runtime), out=tmp_path / "out",
                      settings=Settings(minutes=5, fix_rounds=0)).run({"id": f"hasty-{runtime}"})
    turn = record["turns"][0]
    assert turn["stop_blocks"] >= 1, turn
    assert turn["tool_calls"].get("check", 0) >= 1, turn
    assert record["passed"] and turn["proof"], turn
