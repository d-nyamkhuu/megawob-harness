"""Example domain: rebuild a screenshot as a static page (index.html and style.css), HTML and CSS only.

It needs nothing but Playwright's Chromium, and it has the same parts as a real domain:

- **Tools** (run outside the agent's sandbox): `render` shows the target beside the page, with the similarity
  score, page errors and network requests; `check` (from the harness) runs the quick check.
- **Quick check:** the page loads with no console or page errors and no network requests, has no horizontal
  scroll at 800 px, and its render is close enough to the target (`threshold`).
- **Gate:** the quick check, plus guards against the shortcuts a model might take: no raster images, no data URIs,
  no scripts. A page that pastes the screenshot would score 1.0 on similarity and teach nothing.
- **Review:** a reviewer runtime compares the target and the render and scores fidelity 1-5.

Similarity (`similarity`) is coarse on purpose: the quick check only keeps out blank and half-built pages, and the
reviewer judges how close the page looks.
"""
from __future__ import annotations

import io
import re
import shutil
import threading
from pathlib import Path

from agent_harness.domain import REVIEW_SCHEMA, Domain
from agent_harness.tools import Image

HERE = Path(__file__).resolve().parent
SIZE = (160, 100)
RASTER = re.compile(r"\.(png|jpe?g|gif|webp|avif|bmp)\b|data:", re.I)

BRIEF = """You are rebuilding a web page from a screenshot. The attached image is the target: {width}x{height} px.

Work in this folder: index.html and style.css (both exist as stubs). Use HTML and CSS only: no images, no data URIs,
no scripts, no web fonts or other network requests. Draw everything with HTML and CSS (borders, backgrounds,
gradients, inline SVG is fine).

Tools:
- render(view="compare") shows the target beside your page at the target's size, with a similarity score (0 = only
  the background colour, 1 = pixel-perfect), page errors and network requests. view="narrow" renders at 800 px wide
  to check that nothing overflows. crop=[x, y, w, h] (target pixels) zooms into both.
{check_tool}
Match the layout, spacing, type sizes, colours and text of the target as closely as you can within {minutes}
minutes. Render often.{check_rule}

End with only this JSON object:
{schema}"""

CHECK_TOOL = "- check() runs the acceptance checks and gives you a check_token when they pass.\n"

REVIEW = """The first image is the target design, the second is a page rebuilt from it. Score how faithfully the page
reproduces the target (5 = indistinguishable at a glance, 3 = right layout with clear differences in spacing, type or
colour, 1 = a different page). List each remaining difference as one concrete instruction (what, where, how it
differs)."""

STUB_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><link rel="stylesheet" href="style.css"><title>Page</title></head>
<body>
</body>
</html>
"""


def similarity(target, render) -> float:
    """0 for a page of the target's background colour alone, 1 for a pixel-perfect page: the mean of a colour score
    (mean absolute difference at 160x100) and a structure score (overlap of blurred edge maps), each rescaled
    between those two ends. Coarse on purpose: it keeps out blank and half-built pages; the reviewer judges looks."""
    from PIL import Image as PILImage
    from PIL import ImageChops, ImageFilter, ImageStat

    def small(im):
        return im.convert("RGB").resize(SIZE, PILImage.Resampling.LANCZOS)

    def edges(im):
        return (im.convert("L").filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(6))
                .resize(SIZE, PILImage.Resampling.BOX))

    def colour(x, y) -> float:
        return 1 - sum(ImageStat.Stat(ImageChops.difference(small(x), small(y))).mean) / (3 * 255)

    def structure(x, y) -> float:
        a, b = edges(x), edges(y)
        overlap, union = ImageChops.darker(a, b), ImageChops.lighter(a, b)
        return ImageStat.Stat(overlap).sum[0] / max(1.0, ImageStat.Stat(union).sum[0])

    def rescaled(score, x, y, blank) -> float:
        base = score(x, blank)
        return 1.0 if base >= 1 else max(0.0, min(1.0, (score(x, y) - base) / (1 - base)))

    target, render = target.convert("RGB"), render.convert("RGB")
    blank = PILImage.new("RGB", target.size, max(small(target).getcolors(SIZE[0] * SIZE[1]))[1])
    return (rescaled(colour, target, render, blank) + rescaled(structure, target, render, blank)) / 2


class Renderer:
    """A Chromium page on the workspace's index.html, with what went wrong while loading it."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def shot(self, index: Path, width: int, height: int) -> tuple[bytes, list[str], bool]:
        """(PNG, problems, overflows) for the page at width x height."""
        page = self._browser.new_page(viewport={"width": width, "height": height})
        problems: list[str] = []
        page.on("console", lambda m: m.type == "error" and problems.append(f"console error: {m.text}"[:300]))
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"[:300]))
        page.on("request", lambda r: not r.url.startswith(("file:", "data:", "about:")) and
                problems.append(f"network request: {r.url}"[:300]))
        page.on("requestfailed", lambda r: problems.append(f"failed to load: {r.url}"[:300]))
        try:
            page.goto(index.resolve().as_uri(), wait_until="load")
            png = page.screenshot()
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        finally:
            page.close()
        return png, problems, overflow

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


_local = threading.local()


def renderer() -> Renderer:
    """This thread's renderer. Playwright's sync API allows one instance per thread, and the tool server runs
    every tool on one thread, so `render` and `check` share it."""
    if not hasattr(_local, "renderer"):
        _local.renderer = Renderer()
    return _local.renderer


class StaticPage(Domain):
    """config: root (workspaces, default out/static_page), threshold (quick-check similarity, default 0.5),
    mention_check (default true; false leaves check() out of the brief, to see the Stop gate make the agent use it)."""

    name = "static-page"
    ignore = ("NOTES.md",)

    def _root(self) -> Path:
        return Path(self.config.get("root", "out/static_page"))

    def _target(self, job: dict) -> Path:
        path = Path(job["target"])
        return path if path.is_absolute() else HERE / path

    def _size(self, job: dict) -> tuple[int, int]:
        from PIL import Image as PILImage
        with PILImage.open(self._target(job)) as im:
            return im.size

    def workspace(self, job: dict) -> Path:
        return self._root() / job["id"]

    def prepare(self, job: dict) -> None:
        ws = self.workspace(job)
        ws.mkdir(parents=True)
        (ws / "index.html").write_text(STUB_HTML)
        (ws / "style.css").write_text("body { margin: 0; }\n")

    def brief(self, job: dict, minutes: int) -> str:
        import json
        w, h = self._size(job)
        mention = self.config.get("mention_check", True)
        return BRIEF.format(
            width=w, height=h, minutes=minutes, schema=json.dumps(self.report_schema, indent=1),
            check_tool=CHECK_TOOL if mention else "",
            check_rule=" Before you finish, check() must pass on your final files." if mention else "")

    def images(self, job: dict) -> list[Path]:
        return [self._target(job)]

    def _measure(self, job: dict, renderer: Renderer) -> tuple[float, list[str], bytes]:
        from PIL import Image as PILImage
        w, h = self._size(job)
        index = self.workspace(job) / "index.html"
        png, problems, _ = renderer.shot(index, w, h)
        _, narrow_problems, overflow = renderer.shot(index, 800, h)
        if overflow:
            problems.append("the page scrolls sideways at 800 px wide")
        problems += [p for p in narrow_problems if p not in problems]
        with PILImage.open(self._target(job)) as target:
            score = similarity(target, PILImage.open(io.BytesIO(png)))
        return score, problems, png

    def tools(self, job: dict):
        domain = self

        def render(view: str = "compare", crop: list[int] | None = None) -> list:
            """Render the page. view: "compare" (the target beside the page at the target's size, scaled to half;
            with the similarity score, page errors and network requests) or "narrow" (the page at 800 px wide).
            crop: [x, y, w, h] in target pixels: that region of both, at full size."""
            from PIL import Image as PILImage
            w, h = domain._size(job)
            index = domain.workspace(job) / "index.html"
            if view == "narrow":
                png, problems, overflow = renderer().shot(index, 800, h)
                note = ["the page scrolls sideways at 800 px" if overflow else "no sideways scroll", *problems]
                return [Image(data=png, format="png"), "\n".join(note)]
            score, problems, png = domain._measure(job, renderer())
            left, right = PILImage.open(domain._target(job)).convert("RGB"), PILImage.open(io.BytesIO(png))
            if crop:
                x, y, cw, ch = crop
                left, right = left.crop((x, y, x + cw, y + ch)), right.crop((x, y, x + cw, y + ch))
            else:
                left, right = left.resize((w // 2, h // 2)), right.resize((w // 2, h // 2))
            sheet = PILImage.new("RGB", (left.width * 2 + 8, left.height), "white")
            sheet.paste(left, (0, 0))
            sheet.paste(right, (left.width + 8, 0))
            buf = io.BytesIO()
            sheet.save(buf, "PNG")
            text = [f"left: target, right: your page. similarity {score:.3f}", *problems] or []
            return [Image(data=buf.getvalue(), format="png"), "\n".join(text)]

        return [render]

    def quick_check(self, job: dict) -> list[str]:
        score, problems, _ = self._measure(job, renderer())
        threshold = float(self.config.get("threshold", 0.5))
        if score < threshold:
            problems.append(f"similarity {score:.3f} is below {threshold}: render(view='compare') shows where")
        return problems

    def gate(self, job: dict) -> list[str]:
        ws, fails = self.workspace(job), []
        for path in sorted(ws.rglob("*")):
            if path.is_file() and path.suffix.lower() not in {".html", ".css", ".svg", ".md"}:
                fails.append(f"{path.relative_to(ws)}: only HTML, CSS and SVG files are allowed")
            elif path.suffix in {".html", ".css"}:
                text = path.read_text(errors="replace")
                if RASTER.search(text):
                    fails.append(f"{path.relative_to(ws)} refers to a raster image or a data URI")
                if "<script" in text.lower():
                    fails.append(f"{path.relative_to(ws)} has a script")
        return fails or self.quick_check(job)

    def review(self, job: dict, reviewer) -> dict | None:
        import tempfile
        w, h = self._size(job)
        png, _, _ = renderer().shot(self.workspace(job) / "index.html", w, h)
        with tempfile.TemporaryDirectory(prefix="review-") as tmp:
            shot = Path(tmp) / "page.png"
            shot.write_bytes(png)
            return reviewer.ask(REVIEW, REVIEW_SCHEMA, images=[self._target(job), shot])

    def finalize(self, job: dict, record: dict) -> None:
        notes = self.workspace(job) / "NOTES.md"
        if notes.is_file():
            shutil.move(notes, self._root() / f"{job['id']}.NOTES.md")
