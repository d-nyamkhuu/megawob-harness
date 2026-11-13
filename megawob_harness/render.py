"""Render a site in headless Chromium and compose the shots with its design image.

`shots` runs the renders in a child process of its own (`python -m megawob_harness.render <job.json>`): when it
returns, the browser and all its memory are gone, and a hung browser is killed with it after a timeout. The build
agent's render tool (tools.py) and the site review (build.py) use it; a Renderer is for one thread of that process
(Playwright's sync API is bound to the thread that started it).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

from megawob import catalog, check
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from . import gate

LABEL, SCALE, GAP, BACKGROUND = 28, 0.6, 12, (40, 40, 40)
SMALL = [(1280, 720), (1024, 640)]
# The instruction display and finish settings a frame must handle, at the tightest training viewport.
MODES = [("instruction header", {"instructionDisplay": "header", "finishByMessage": "0"}),
         ("instruction panel", {"instructionDisplay": "panel", "finishByMessage": "0"}),
         ("instruction toast", {"instructionDisplay": "toast", "finishByMessage": "0"}),
         ("instruction hidden, finish by message", {"instructionDisplay": "hidden", "finishByMessage": "1"})]
MODES_SIZE = (1024, 640)

# What the checker would fail on, reported as layout errors: the end control off-screen or covered, and the task
# stage running past the viewport.
LAYOUT_JS = """() => {
  const e = window.__env; if (!e || !e.ready) return [];
  const W = innerWidth, H = innerHeight, out = [];
  const desc = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
    + (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
  const s = e.submitEl;
  if (s) {
    const r = s.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (r.width < 4 || r.height < 4) out.push('end control has no visible size');
    else if (x < 0 || y < 0 || x > W || y > H) out.push(`end control centre (${x | 0},${y | 0}) is outside the ${W}x${H} viewport`);
    else { const hit = document.elementFromPoint(x, y);
      if (hit && hit !== s && !s.contains(hit)) out.push(`end control is covered by ${desc(hit)} at (${x | 0},${y | 0})`); }
  }
  const st = e.stage && e.stage.getBoundingClientRect();
  if (st && !(e.episode && e.episode.scrollable) && (st.bottom > H + 1 || st.right > W + 1))
    out.push(`task stage ${st.width | 0}x${st.height | 0} at (${st.left | 0},${st.top | 0}) runs past the ${W}x${H} viewport`);
  return out;
}"""


class Renderer:
    def __init__(self, site: str, task: str, scenario: str, image: Path):
        self.widget = catalog.Widget(task, site, "design", "train")
        self.scenario, self.image = scenario, Path(image)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch()
        self.notes: list[str] = []  # the oracle actions of the last play

    def __enter__(self) -> Renderer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.browser.close()
        self._pw.stop()

    def capture(self, seed: int, size: tuple[int, int], popups: str | None = None,
                params: dict | None = None) -> tuple[Image.Image, list[str]]:
        params = dict(params or {})
        play = int(params.pop("play", 0) or 0)
        url = catalog.page_url(self.widget, seed, scenario=self.scenario, base=catalog.HTML.resolve().as_uri() + "/",
                               **params)
        if popups:  # that popup, open from the first step
            url += f"&popups={popups}&popupAt=0"
        errors: list[str] = []
        context = self.browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
        try:
            page = context.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.on("console", lambda msg: msg.type == "error" and errors.append(f"console: {msg.text}"))
            page.goto(url)
            try:
                page.wait_for_function("() => window.__env && (window.__env.ready || window.__env.bootError)",
                                       timeout=15000)
                boot = page.evaluate("() => window.__env.bootError")
                if boot:
                    errors.append(f"bootError: {boot}")
            except Exception as exc:  # noqa: BLE001 - report it, and still show whatever rendered
                errors.append(f"boot timeout: {exc}".splitlines()[0])
            page.wait_for_timeout(250)  # fonts and images settle
            if play:
                self._play(page, play, f"{size[0]}x{size[1]}")
            if not popups:  # a popup may cover the end control
                try:
                    errors += [f"layout: {e}" for e in page.evaluate(LAYOUT_JS)]
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"layout check failed: {exc}".splitlines()[0])
            shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        finally:
            context.close()
        return shot, errors

    def _play(self, page, n: int, where: str) -> None:
        """Follow the oracle for n actions, as megawob.check does, noting each action and the status after it."""
        for i in range(n):
            plan = page.evaluate("() => { try { return window.__env.taskDef.plan(window.__env.model, window.__env.spec); }"
                                 " catch (e) { return 'plan threw: ' + e; } }")
            action = page.evaluate("() => window.__env.oracle()")
            if action is None:
                self.notes.append(f"{where} action {i + 1}: the oracle has nothing to do")
                break
            check.act(page, action, 8)
            st = page.evaluate("() => { const s = window.__env.status(); "
                               "return {done: s.done, success: s.success, notice: s.notice || null}; }")
            self.notes.append(f"{where} action {i + 1}: plan {plan} -> {action} -> done={st['done']} "
                              f"success={st['success']}" + (f" notice={st['notice']!r}" if st["notice"] else ""))
            if st["done"]:
                break
        page.wait_for_timeout(150)

    def compare(self, seed: int = 0, view: str = "design", crop: tuple[int, int, int, int] | None = None,
                popups: str | None = None, params: dict | None = None) -> tuple[Image.Image, list[str]]:
        """view "design": design image | render at the image's size (scaled to 0.6, or one region at full size with
        crop); "small": renders at 1280x720 and 1024x640; "modes": the four MODES at 1024x640."""
        self.notes = []
        params = params or {}
        if view in ("modes", "small"):
            shots = ([(name, MODES_SIZE, {**params, **mode}) for name, mode in MODES] if view == "modes"
                     else [("render", size, params) for size in SMALL])
            panels, errors = [], []
            for name, size, shot_params in shots:
                label = f"{name}  {size[0]}x{size[1]}"
                img, errs = self.capture(seed, size, popups, shot_params)
                panels.append((label, img))
                errors += [f"{label}: {e}" for e in errs]
            return grid(panels, 2), errors
        ref = Image.open(self.image).convert("RGB")
        img, errors = self.capture(seed, ref.size, popups, params)
        if crop:
            x, y, w, h = crop
            box = (x, y, x + w, y + h)
            return side_by_side([("design image (crop)", ref.crop(box)), ("render (crop)", img.crop(box))]), errors
        scaled = [(name, im.resize((round(im.width * SCALE), round(im.height * SCALE)), Image.LANCZOS))
                  for name, im in (("design image", ref), ("render", img))]
        return side_by_side(scaled, note=f"both scaled to {SCALE}; native {ref.width}x{ref.height}"), errors


def side_by_side(panels: list[tuple[str, Image.Image]], note: str = "") -> Image.Image:
    out = Image.new("RGB", (sum(im.width for _, im in panels) + GAP * (len(panels) - 1),
                            max(im.height for _, im in panels) + LABEL), BACKGROUND)
    draw, x = ImageDraw.Draw(out), 0
    for name, im in panels:
        out.paste(im, (x, LABEL))
        draw.text((x + 6, 8), f"{name}  {note}".strip(), fill=(255, 255, 255))
        x += im.width + GAP
    return out


def grid(panels: list[tuple[str, Image.Image]], cols: int) -> Image.Image:
    rows = [side_by_side(panels[i:i + cols]) for i in range(0, len(panels), cols)]
    out = Image.new("RGB", (max(r.width for r in rows), sum(r.height for r in rows) + GAP * (len(rows) - 1)),
                    BACKGROUND)
    y = 0
    for r in rows:
        out.paste(r, (0, y))
        y += r.height + GAP
    return out


def shots(site: str, task: str, scenario: str, image: Path, requests: list[dict],
          timeout: int = 180) -> list[tuple[Image.Image, list[str], list[str]]]:
    """(image, errors, oracle notes) of each request's compare() (keys: seed, view, crop, popups, params), rendered
    in a child process that is killed with its browser if it takes longer than `timeout` seconds."""
    with tempfile.TemporaryDirectory(prefix="megawob-harness-render-") as tmp:
        job = Path(tmp) / "job.json"
        job.write_text(json.dumps({"site": site, "task": task, "scenario": scenario,
                                   "image": str(Path(image).resolve()), "requests": requests}))
        out = gate.run_module("megawob_harness.render", str(job), timeout=timeout)
        results = Path(tmp) / "results.json"
        if out.returncode or not results.is_file():
            why = "timed out" if out.returncode == -9 else f"exit {out.returncode}"
            raise RuntimeError(f"render failed ({why}): {(out.stderr or out.stdout).strip()[-300:]}")
        return [(Image.open(Path(tmp) / f"{i}.png").convert("RGB"), r["errors"], r["notes"])
                for i, r in enumerate(json.loads(results.read_text()))]


def main(argv: list[str] | None = None) -> None:
    """The renders of one job.json, written next to it: <i>.png per request, and results.json."""
    path = Path((sys.argv[1:] if argv is None else argv)[0])
    job, results = json.loads(path.read_text()), []
    with Renderer(job["site"], job["task"], job["scenario"], Path(job["image"])) as r:
        for i, q in enumerate(job["requests"]):
            crop = tuple(q["crop"]) if q.get("crop") else None
            img, errors = r.compare(q.get("seed", 0), q.get("view", "design"), crop, q.get("popups"), q.get("params"))
            img.save(path.parent / f"{i}.png")
            results.append({"errors": errors, "notes": r.notes})
    (path.parent / "results.json").write_text(json.dumps(results))


if __name__ == "__main__":
    main()
