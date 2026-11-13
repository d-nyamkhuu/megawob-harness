"""The command line's batch handling: selection, pending designs, dry runs, status, and stopping on Ctrl-C."""
import argparse
import json
import os
import threading
import time

import pytest

pytest.importorskip("megawob")
from megawob_harness import cli  # noqa: E402

SCENARIOS = {"set-slider": ["horizontal", "vertical"], "use-email": ["archive"], "no-card": ["only"]}


@pytest.fixture
def tasks(monkeypatch, tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    for task in ("set-slider", "use-email"):
        (cards / f"{task}.json").write_text("{}")
    monkeypatch.setattr(cli.catalog, "tasks", lambda: sorted(SCENARIOS))
    monkeypatch.setattr(cli.designs, "scenarios", lambda task: SCENARIOS[task])
    monkeypatch.setattr(cli.designs, "card_path", lambda task: cards / f"{task}.json")


def test_pairs_default_to_every_scenario_of_the_task(tasks):
    assert cli.pairs("set-slider", [], False) == [("set-slider", "horizontal"), ("set-slider", "vertical")]
    assert cli.pairs("set-slider", ["vertical"], False) == [("set-slider", "vertical")]


def test_all_covers_every_task_with_a_card(tasks):
    assert cli.pairs(None, [], True) == [("set-slider", "horizontal"), ("set-slider", "vertical"),
                                         ("use-email", "archive")]


@pytest.mark.parametrize("task, scenarios, every", [
    (None, [], False), ("set-slider", [], True), ("set-slider", ["diagonal"], False)])
def test_bad_selections_stop_before_planning(tasks, task, scenarios, every):
    with pytest.raises(SystemExit):
        cli.pairs(task, scenarios, every)


def test_pending_is_accepted_designs_without_a_site(monkeypatch, tmp_path):
    html, out = tmp_path / "html", tmp_path / "out"
    monkeypatch.setattr(cli.catalog, "HTML", html)
    designs = {"01": "accepted", "02": "rejected", "03": "accepted", "04": "accepted", "05": "accepted"}
    for nn, state in designs.items():
        folder = out / "designs" / "set-slider" / "horizontal" / nn
        folder.mkdir(parents=True)
        (folder / "design.json").write_text(json.dumps({"site": f"design-set-slider-horizontal-{nn}", "status": state}))
    (html / "sites" / "design-set-slider-horizontal-03").mkdir(parents=True)  # built: it has a run record
    (out / "runs").mkdir()
    (out / "runs" / "design-set-slider-horizontal-03.json").write_text("{}")
    (html / "sites" / "design-set-slider-horizontal-04").mkdir()  # no run record: may be built elsewhere
    (html / "sites" / "design-set-slider-horizontal-05").mkdir()  # its build stopped on an error
    (out / "runs" / "design-set-slider-horizontal-05.json").write_text('{"error": "AgentError: rate limited"}')
    assert [p.parent.name for p in cli.unbuilt(out)] == ["01", "05"]


def test_undrawn_is_planned_designs_without_a_status(tmp_path):
    for nn, state in {"01": None, "02": "accepted", "03": "failed"}.items():
        folder = tmp_path / "designs" / "set-slider" / "horizontal" / nn
        folder.mkdir(parents=True)
        (folder / "design.json").write_text(json.dumps({"site": f"s-{nn}"} | ({"status": state} if state else {})))
    assert [p.parent.name for p in cli.undrawn(tmp_path)] == ["01"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    args = argparse.Namespace(out=tmp_path / "out", count=2, style=None)
    cli.dry_run(args, [("set-slider", "horizontal")])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and lines[0].split()[1] != lines[1].split()[1]  # two designs, different styles
    assert "2 designs would be planned" in lines[2]
    assert not (tmp_path / "out").exists()


def test_status_summarises_the_builds(tmp_path, capsys):
    turn = {"stage": "build"}
    for nn, state, record in [("01", "accepted", {"passed": True, "minutes": 10, "fidelity": 4, "turns": [turn]}),
                              ("02", "accepted", {"passed": True, "minutes": 30, "fidelity": 3,
                                                  "turns": [turn, {"stage": "build fix"}]}),
                              ("03", "failed", None), ("04", "accepted", {"passed": False, "error": "x", "turns": []})]:
        folder = tmp_path / "designs" / "set-slider" / "horizontal" / nn
        folder.mkdir(parents=True)
        (folder / "design.json").write_text(json.dumps({"site": f"s-{nn}", "status": state}))
        if record:
            (tmp_path / "runs").mkdir(exist_ok=True)
            (tmp_path / "runs" / f"s-{nn}.json").write_text(json.dumps(record))
    cli.status(tmp_path)
    out = capsys.readouterr().out
    assert "4 designs: 3 accepted, 1 failed" in out
    assert "1 builds stopped on an error" in out
    assert "2 built: 2 passed the gate (100%), 1 of them without a fix round; median 20.0 min" in out
    assert "mean review fidelity 3.5" in out


def test_ctrl_c_starts_no_new_design(monkeypatch, tmp_path):
    started = []

    def fake_build(path, *args):
        started.append(path)
        if len(started) == 1:
            raise KeyboardInterrupt  # stands in for Ctrl-C reaching the main thread
        return {"site": "s", "passed": True}

    monkeypatch.setattr(cli.build, "build", fake_build)
    monkeypatch.setattr(cli, "get_runtime", lambda *a: None)
    monkeypatch.setattr(cli.gate, "_stopped", threading.Event())  # run() stops the gate; keep that to this test
    paths = [tmp_path / f"{i}.json" for i in range(5)]
    with pytest.raises(SystemExit) as exc:
        cli.run(batch_args(tmp_path), paths, draw=False, make=True)
    assert exc.value.code == 130 and len(started) == 1


def batch_args(out, jobs=1):
    return argparse.Namespace(out=out, cmd="build", runtime="codex", review_runtime="codex", model=None,
                              review_model=None, polish_model=None, no_polish=True, effort=None, jobs=jobs, minutes=1,
                              fix_rounds=0, seeds=1, force=False, fix_model=None, image_model=None, attempts=1)


def test_too_many_jobs_for_the_free_memory_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "get_runtime", lambda *a: None)
    monkeypatch.setattr(cli, "available_gb", lambda: 6.5)
    cli.run(batch_args(tmp_path, jobs=4), [], draw=False, make=True)
    assert "warning" not in capsys.readouterr().out
    cli.run(batch_args(tmp_path, jobs=8), [], draw=False, make=True)
    assert "warning: 8 jobs may need about 8 GB and 6.5 GB is free; lower --jobs (about 6)" in capsys.readouterr().out
    assert cli.available_gb() is None or cli.available_gb() > 0  # real /proc/meminfo, where there is one


def test_stop_kills_running_checkers_and_starts_no_more(monkeypatch):
    from megawob_harness import gate

    monkeypatch.setattr(gate, "_running", set())
    monkeypatch.setattr(gate, "_stopped", threading.Event())
    result = {}
    server = ("http.server", "0", "--bind", "127.0.0.1")
    worker = threading.Thread(target=lambda: result.update(r=gate.run_module(*server)))
    worker.start()  # a stand-in checker that never ends by itself
    deadline = time.monotonic() + 10
    while not gate._running and time.monotonic() < deadline:
        time.sleep(0.05)
    (pid,) = gate._running
    gate.stop()
    worker.join(10)
    assert not worker.is_alive() and result["r"].returncode == -9
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)  # the whole process group is gone
    assert gate.run_module("http.server", "0").stderr == "stopped"
