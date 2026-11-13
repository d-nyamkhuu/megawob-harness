import json

import pytest

from agent_harness import Pipeline, Settings
from agent_harness.pipeline import NO_PROOF
from agent_harness.runtime.replay import ReplayRuntime
from tests.toy_domain import Toy

JOB = {"id": "job-1"}


def report(token=None, done=True):
    return json.dumps({"done": done, "summary": "s", "remaining": [], "check_token": token})


def write(ctx, text):
    (ctx.cwd / "answer.txt").write_text(text)


def token_of(result: str):
    return result.split("check_token: ")[1].strip() if "check_token: " in result else None


def finish(text="42\n"):
    """Write the answer, check it, report with the token."""
    def step(ctx):
        write(ctx, text)
        return report(token_of(ctx.call("check")))
    return step


def claim(text="42\n"):
    """Write the answer and claim done without checking."""
    def step(ctx):
        write(ctx, text)
        return report(None)
    return step


def recheck(ctx):
    return report(token_of(ctx.call("check")))


@pytest.fixture
def toy(tmp_path):
    return Toy(root=str(tmp_path / "ws"))


def run(toy, tmp_path, builder, settings=None, **kw):
    return Pipeline(toy, builder, settings=settings or Settings(), out=tmp_path / "out", **kw).run(JOB)


def test_checked_work_passes_in_one_turn(toy, tmp_path):
    rec = run(toy, tmp_path, ReplayRuntime([finish()]))
    assert rec["passed"] and len(rec["turns"]) == 1
    turn = rec["turns"][0]
    assert turn["proof"] and turn["stop_blocks"] == 0 and turn["tool_calls"] == {"check": 1}
    assert json.loads((tmp_path / "out" / "runs" / "job-1.json").read_text())["passed"]


def test_stop_gate_makes_the_agent_check_inside_the_turn(toy, tmp_path):
    builder = ReplayRuntime([claim(), recheck])
    rec = run(toy, tmp_path, builder)
    assert rec["passed"] and len(rec["turns"]) == 1  # no fix round needed
    assert rec["turns"][0]["stop_blocks"] == 1
    assert "You have not called check() yet" in builder.prompts[1]


def test_without_the_gate_an_unchecked_claim_costs_a_fix_round(toy, tmp_path):
    rec = run(toy, tmp_path, ReplayRuntime([claim(), recheck]), Settings(stop_gate=False))
    assert rec["passed"] and len(rec["turns"]) == 2
    assert rec["turns"][0]["gate"] == [NO_PROOF] and not rec["turns"][0]["proof"]


def test_gate_failures_go_back_as_a_fix_round_on_the_fix_model(toy, tmp_path):
    builder = ReplayRuntime([finish("42"), finish("42\n")])  # the quick check passes, the gate wants a newline
    rec = run(toy, tmp_path, builder, fix_model="stronger")
    assert rec["passed"]
    assert [t["stage"] for t in rec["turns"]] == ["build", "build fix"]
    assert rec["turns"][0]["gate"] == ["answer.txt does not end with a newline"]
    assert rec["turns"][1]["model"] == "stronger"
    assert "answer.txt does not end with a newline" in builder.prompts[1]


def test_a_failed_check_blocks_the_stop_until_fixed(toy, tmp_path):
    builder = ReplayRuntime([finish("41\n"), finish("42\n")])
    rec = run(toy, tmp_path, builder)
    assert rec["passed"] and len(rec["turns"]) == 1 and rec["turns"][0]["stop_blocks"] == 1
    assert "Your last check() failed" in builder.prompts[1] and "not 42" in builder.prompts[1]


def test_editing_after_the_check_voids_the_token(toy, tmp_path):
    def sneaky(ctx):
        write(ctx, "42\n")
        token = token_of(ctx.call("check"))
        write(ctx, "42\n\n")
        return report(token)
    rec = run(toy, tmp_path, ReplayRuntime([sneaky, recheck]), Settings(stop_gate=False, fix_rounds=1))
    assert not rec["turns"][0]["proof"] and rec["turns"][1]["proof"] and rec["passed"]


def test_ignored_files_do_not_void_the_token(toy, tmp_path):
    def notes(ctx):
        write(ctx, "42\n")
        token = token_of(ctx.call("check"))
        (ctx.cwd / "NOTES.md").write_text("a kit request")
        return report(token)
    assert run(toy, tmp_path, ReplayRuntime([notes]))["turns"][0]["proof"]


def test_the_gate_gives_up_after_max_blocks(toy, tmp_path):
    builder = ReplayRuntime([claim()] * 4)
    rec = run(toy, tmp_path, builder, Settings(fix_rounds=0, max_stop_blocks=3))
    assert not rec["passed"] and rec["turns"][0]["stop_blocks"] == 3 and not builder.steps


def test_not_done_is_not_accepted(toy, tmp_path):
    def unfinished(ctx):
        write(ctx, "42\n")
        return report(token_of(ctx.call("check")), done=False)
    rec = run(toy, tmp_path, ReplayRuntime([unfinished, recheck]))
    assert rec["passed"] and len(rec["turns"]) == 2 and "done=false" in rec["turns"][0]["gate"][0]


def test_a_polish_that_scores_lower_is_rolled_back(toy, tmp_path):
    reviewer = ReplayRuntime(answers=[{"fidelity": 3, "differences": ["d"]}, {"fidelity": 2, "differences": []}])
    polisher = ReplayRuntime([lambda ctx: ((ctx.cwd / "extra.txt").write_text("x"), recheck(ctx))[-1]])
    rec = run(toy, tmp_path, ReplayRuntime([finish()]), reviewer=reviewer, polisher=polisher)
    assert rec["passed"] and rec["polish"]["kept"] is False and rec["fidelity"] == 3
    assert not (toy.workspace(JOB) / "extra.txt").exists()


def test_a_polish_that_scores_higher_is_kept(toy, tmp_path):
    reviewer = ReplayRuntime(answers=[{"fidelity": 3, "differences": ["d"]}, {"fidelity": 4, "differences": []}])
    polisher = ReplayRuntime([lambda ctx: ((ctx.cwd / "extra.txt").write_text("x"), recheck(ctx))[-1]])
    rec = run(toy, tmp_path, ReplayRuntime([finish()]), reviewer=reviewer, polisher=polisher)
    assert rec["polish"]["kept"] and rec["fidelity"] == 4 and (toy.workspace(JOB) / "extra.txt").exists()
    assert "- d" in polisher.prompts[0]


def test_a_top_score_skips_the_polish(toy, tmp_path):
    reviewer = ReplayRuntime(answers=[{"fidelity": 5, "differences": []}])
    rec = run(toy, tmp_path, ReplayRuntime([finish()]), reviewer=reviewer, polisher=ReplayRuntime())
    assert "polish" not in rec and rec["fidelity"] == 5


def test_an_existing_workspace_needs_force(toy, tmp_path):
    run(toy, tmp_path, ReplayRuntime([finish()]))
    with pytest.raises(FileExistsError):
        run(toy, tmp_path, ReplayRuntime([finish()]))
