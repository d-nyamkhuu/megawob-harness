import json
import subprocess
import sys
import time

from megawob_harness.checks import Ledger, StopGate, check_tool, tree_hash


def test_tree_hash_follows_content_and_skips_ignored(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    h = tree_hash(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("junk")
    (tmp_path / "NOTES.md").write_text("n")
    assert tree_hash(tmp_path, ["NOTES.md"]) == h
    (tmp_path / "a.txt").write_text("2")
    assert tree_hash(tmp_path) != h


def test_tokens_are_only_valid_for_a_passing_check_of_the_current_files(tmp_path):
    ws, ledger = tmp_path / "ws", Ledger(tmp_path / "state")
    ws.mkdir()
    (ws / "f").write_text("x")
    results = iter([["broken"], []])
    check = check_tool(lambda: next(results), ws, ledger)
    assert check().startswith("FAIL broken")
    token = check().split("check_token: ")[1]
    assert ledger.verify(token, tree_hash(ws))
    assert not ledger.verify("forged", tree_hash(ws)) and not ledger.verify(None, tree_hash(ws))
    (ws / "f").write_text("y")
    assert not ledger.verify(token, tree_hash(ws))


def test_a_crashing_check_is_a_failure(tmp_path):
    def boom():
        raise RuntimeError("no browser")
    out = check_tool(boom, tmp_path, Ledger(tmp_path / "s"))()
    assert out.startswith("FAIL the check crashed: RuntimeError: no browser")


def test_gate_reasons_limits_and_deadline(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    gate = StopGate(ws, tmp_path / "state", max_blocks=2)
    gate.start_turn(60)
    assert "not called check()" in gate.decide()
    check = check_tool(lambda: [], ws, gate.ledger)
    check()
    assert gate.decide() is None
    (ws / "new").write_text("edit")
    assert "changed files after your last check()" in gate.decide()
    assert gate.decide() is None  # two blocks this turn: give up, the external gate still runs
    gate.start_turn(60)
    assert gate.decide() is not None  # the limit is per turn
    gate.start_turn(0)
    time.sleep(0.01)
    assert gate.decide() is None  # past the turn's time limit
    assert gate.blocks() == 3


def test_stop_hook_command_speaks_the_hook_protocol(tmp_path):
    ws, state = tmp_path / "ws", tmp_path / "state"
    ws.mkdir()
    cmd = [sys.executable, "-m", "megawob_harness.checks", "stop-hook", "--workspace", str(ws), "--state", str(state)]
    out = subprocess.run(cmd, input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": False}),
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["decision"] == "block"
    check_tool(lambda: [], ws, Ledger(state))()
    out = subprocess.run(cmd, input="{}", capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == {}
