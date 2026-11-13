"""The build pipeline on a real site, with a scripted agent (no model, no API key).

Each test scaffolds a stand-in design (a set-slider site numbered 99) in the megawob checkout, runs the real gate
(lint and megawob.check, one seed), and removes the site again, restoring html/scenarios.json. They need Node and
Playwright's Chromium, and take about a minute each.
"""
import json
import shutil

import pytest
from PIL import Image

pytest.importorskip("megawob")
from megawob import catalog  # noqa: E402

from megawob_harness import build  # noqa: E402
from megawob_harness.runtime.replay import ReplayRuntime  # noqa: E402

pytestmark = pytest.mark.megawob
SITE = "design-set-slider-horizontal-99"
INDEX = catalog.HTML / "scenarios.json"


@pytest.fixture
def design(tmp_path):
    if (catalog.HTML / "sites" / SITE).exists():
        pytest.skip(f"{SITE} exists in the checkout")
    folder = tmp_path / "designs" / "set-slider" / "horizontal" / "99"
    folder.mkdir(parents=True)
    Image.new("RGB", (1536, 864), "#dde3ea").save(folder / "design.png")
    (folder / "design.json").write_text(json.dumps({
        "id": "set-slider/horizontal/99-test", "site": SITE, "task": "set-slider", "scenario": "horizontal",
        "status": "accepted", "era": "2010s", "style": {"desc": "flat test style"},
        "arrangement": {"navigation": "none"}, "screen": "one horizontal slider", "sample": "set the slider to 5"}))
    index = INDEX.read_bytes()
    yield folder / "design.json"
    shutil.rmtree(catalog.HTML / "sites" / SITE, ignore_errors=True)
    INDEX.write_bytes(index)


def report(done=True):
    return json.dumps({"done": done, "fidelity": 3, "remaining_differences": [], "deviations": [], "notes": ""})


def checked(ctx):
    assert ctx.call("check") == "check passed"
    return report()


def stopped_early(ctx):
    return report(done=False)


def run(design, tmp_path, builder, **kw):
    settings = kw.pop("settings", build.Settings(seeds=1, minutes=5))
    return build.build(design, tmp_path / "out", builder, settings=settings, **kw)


def test_a_checked_build_passes_in_one_turn(design, tmp_path):
    def look_then_check(ctx):
        image, text = ctx.call("render", view="small")
        assert image.data[:4] == b"\x89PNG" and text == "no page errors"
        return checked(ctx)

    rec = run(design, tmp_path, ReplayRuntime([look_then_check]))
    assert rec["passed"] and len(rec["turns"]) == 1
    assert rec["turns"][0]["tool_calls"] == {"render": 1, "check": 1}
    assert json.loads((tmp_path / "out" / "runs" / f"{SITE}.json").read_text())["passed"]


def test_a_first_turn_that_is_not_done_goes_back_even_though_the_scaffold_passes(design, tmp_path):
    builder = ReplayRuntime([stopped_early, stopped_early])
    rec = run(design, tmp_path, builder)
    assert rec["passed"] and [t["stage"] for t in rec["turns"]] == ["build", "build fix"]
    assert rec["turns"][0]["gate"] == [build.NOT_DONE]
    assert rec["turns"][1]["gate"] == []  # a fix round only has to make the gate pass


def test_a_broken_site_goes_back_as_a_fix_round_on_the_fix_model(design, tmp_path):
    site_js = catalog.HTML / "sites" / SITE / "site.js"
    saved = {}

    def break_it(ctx):
        saved["js"] = site_js.read_text()
        site_js.write_text(saved["js"] + "\nenv.site({ name: \n")
        return report()

    def mend(ctx):
        site_js.write_text(saved["js"])
        return checked(ctx)

    builder = ReplayRuntime([break_it, mend])
    rec = run(design, tmp_path, builder, fix_model="stronger")
    assert rec["passed"] and rec["turns"][1]["model"] == "stronger"
    assert any("node --check site.js" in f for f in rec["turns"][0]["gate"])
    assert "node --check site.js" in builder.prompts[1]


def test_a_polish_the_reviewer_scores_lower_is_rolled_back(design, tmp_path):
    css = catalog.HTML / "sites" / SITE / "site.css"
    reviewer = ReplayRuntime(answers=[{"fidelity": 3, "differences": ["header colour"], "frame_issues": []},
                                      {"fidelity": 2, "differences": [], "frame_issues": []}])

    def restyle(ctx):
        css.write_text(css.read_text() + "\n/* polished */\nbody { letter-spacing: 0.01em; }\n")
        return checked(ctx)

    polisher = ReplayRuntime([restyle])
    rec = run(design, tmp_path, ReplayRuntime([checked]), reviewer=reviewer, polisher=polisher)
    assert rec["passed"] and rec["polish"] == {"kept": False, "gate_passed": True, "review": rec["polish"]["review"]}
    assert rec["fidelity"] == 3 and "/* polished */" not in css.read_text()
    assert "- header colour" in polisher.prompts[0]
