"""Renders run in a child process of their own: they leave no browser behind, and a hung one is killed.

Real Chromium on a site of the megawob checkout (no model)."""
import os
import signal
import subprocess
import threading
import time

import pytest
from PIL import Image

pytest.importorskip("megawob")
from megawob import catalog  # noqa: E402

from megawob_harness import gate, tools  # noqa: E402
from megawob_harness.render import shots  # noqa: E402

pytestmark = pytest.mark.megawob
SITE = "design-set-slider-horizontal-01"


def browsers() -> int:
    out = subprocess.run(["pgrep", "-c", "-f", "chrom.*headless|headless_shell"], capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


@pytest.fixture
def spec(tmp_path):
    if not (catalog.HTML / "sites" / SITE).is_dir():
        pytest.skip(f"{SITE} is not in the checkout")
    Image.new("RGB", (1536, 864), "white").save(tmp_path / "design.png")
    return {"site": SITE, "task": "set-slider", "scenario": "horizontal", "image": str(tmp_path / "design.png")}


def test_renders_leave_no_browser_behind(spec):
    before, render = browsers(), tools.build(spec)["render"]
    for view in ("design", "small", "modes"):
        image, text = render(view=view)
        assert image.data[:4] == b"\x89PNG" and "ERROR" not in text
    assert browsers() == before


def test_a_hung_render_is_killed_with_its_browser(spec, monkeypatch):
    monkeypatch.setattr(gate, "_running", set())
    before = browsers()

    def freeze():  # what a hung browser looks like from outside: no progress
        while not gate._running:
            time.sleep(0.05)
        time.sleep(0.8)
        for pid in list(gate._running):
            os.killpg(pid, signal.SIGSTOP)

    threading.Thread(target=freeze, daemon=True).start()
    with pytest.raises(RuntimeError, match="timed out"):
        shots(spec["site"], spec["task"], spec["scenario"], spec["image"], [{"view": "modes"}], timeout=8)
    time.sleep(1)
    assert browsers() == before
