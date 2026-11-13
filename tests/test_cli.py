"""Batch selection in the command line: which (task, scenario) pairs to design and which designs are pending."""
import json

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
    designs = {"01": "accepted", "02": "rejected", "03": "accepted", "04": "accepted"}
    for nn, state in designs.items():
        folder = out / "designs" / "set-slider" / "horizontal" / nn
        folder.mkdir(parents=True)
        (folder / "design.json").write_text(json.dumps({"site": f"design-set-slider-horizontal-{nn}", "status": state}))
    (html / "sites" / "design-set-slider-horizontal-03").mkdir(parents=True)  # built: it has a run record
    (out / "runs").mkdir()
    (out / "runs" / "design-set-slider-horizontal-03.json").write_text("{}")
    (html / "sites" / "design-set-slider-horizontal-04").mkdir()  # interrupted: a site but no run record
    assert [p.parent.name for p in cli.pending(out)] == ["01"]
