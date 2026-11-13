"""The example domain's checks, on hand-written pages (no model)."""
import pytest

pytest.importorskip("playwright")
pytestmark = pytest.mark.browser

from examples.static_page.domain import StaticPage  # noqa: E402

JOB = {"id": "p", "target": "targets/pricing.png"}


@pytest.fixture
def page(tmp_path):
    domain = StaticPage(root=str(tmp_path))
    domain.prepare(JOB)
    return domain


def write(domain, html, css=""):
    (domain.workspace(JOB) / "index.html").write_text(html)
    (domain.workspace(JOB) / "style.css").write_text(css)


def test_the_stub_is_rejected(page):
    assert any("similarity 0.000" in f for f in page.gate(JOB))


def test_pasting_the_screenshot_is_caught_by_the_gate(page):
    write(page, '<img src="../../targets/pricing.png">')
    assert any("raster image" in f for f in page.gate(JOB))


def test_scripts_network_and_overflow_are_reported(page):
    write(page, '<script>console.error("x")</script><link rel="stylesheet" href="https://example.com/a.css">'
                '<div style="width:1200px">wide</div>')
    fails = " ".join(page.gate(JOB))
    assert "has a script" in fails
    fails = " ".join(page.quick_check(JOB))
    assert "console error" in fails and "network request" in fails and "sideways" in fails


def test_render_tool_returns_a_comparison(page):
    render = page.tools(JOB)[0]
    image, text = render()
    assert image.data[:4] == b"\x89PNG" and text.startswith("left: target, right: your page. similarity 0.000")
