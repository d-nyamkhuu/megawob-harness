"""Capability cards: where they are read from and written to."""
import json

import pytest

pytest.importorskip("megawob")
from megawob_harness import designs  # noqa: E402
from megawob_harness.runtime.replay import ReplayRuntime  # noqa: E402

CARD = {"identity": "a slider", "scenarios": [
    {"name": "horizontal", "screen": "s", "scene": "", "never": [], "arrangements": [{"text": "t", "traits": "r"}]}]}


def test_cards_are_written_to_out_and_preferred_over_the_installed_ones(monkeypatch, tmp_path):
    installed = tmp_path / "package"
    installed.mkdir()
    (installed / "set-slider.json").write_text(json.dumps(
        {"task": "set-slider", **CARD, "scenarios": [{**CARD["scenarios"][0], "hint": "hand-written"}]}))
    monkeypatch.setattr(designs, "CARDS", installed)
    out = tmp_path / "out"
    assert designs.card_path("set-slider", out) == installed / "set-slider.json"  # nothing in <out> yet

    for name, value in {"catalog_row": lambda task, n: {"description": "", "scenarios": {"horizontal": ["g"]}},
                        "task_doc": lambda task: "", "task_index": lambda task: {},
                        "scenarios": lambda task: ["horizontal"]}.items():
        monkeypatch.setattr(designs, name, value)
    path = designs.make_card(ReplayRuntime(answers=[CARD]), "set-slider", out)

    assert path == out / "cards" / "set-slider.json" and designs.card_path("set-slider", out) == path
    assert designs.load_card("set-slider", out)["scenarios"][0]["hint"] == "hand-written"  # kept from the old card
    assert "hint" in json.loads((installed / "set-slider.json").read_text())["scenarios"][0]  # the package untouched
