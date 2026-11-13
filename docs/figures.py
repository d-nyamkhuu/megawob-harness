"""Regenerate the README figures in docs/media, all in one house style.

    python docs/figures.py diagrams    # pipeline, gate and architecture (PNG)
    cd path/to/megawob && python path/to/megawob-harness/docs/figures.py shots    # screenshots of built sites (WebP)

Every figure is an HTML page (IBM Plex, a navy and a coral accent, thin grey rules) screenshotted by Playwright at 2x:
navy marks deterministic code, coral marks what a model does.

- pipeline.png: the three stages, their loops and which model tier works where.
- gate.png: what a site must pass before it is accepted, with the five viewports drawn to scale.
- architecture.png: what the agent can write, and what judges it from outside its reach.
- collage.webp: one built site per task, across style eras.
- one-task-four-designs.webp: four built sites of one task and scenario, same seed.
- render-modes.webp: what the build agent's `render(view="modes")` tool returns.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from html import escape
from pathlib import Path

MEDIA = Path(__file__).resolve().parent / "media"
WIDTH = 888  # content width; the figure adds 36px on each side
FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
         "&family=IBM+Plex+Sans:wght@400;500;600;700&display=block")
CSS = """
:root { --ink: #1d2b3a; --muted: #5d6877; --rule: #cfd6df; --grid: #e9edf2; --dash: #8b95a3;
        --navy: #2b4c7e; --coral: #cf6a5c; --navy-tint: #f3f6fa; --coral-tint: #fcf3f1; }
* { box-sizing: border-box; margin: 0; }
body { background: #fff; }
#figure { width: 960px; padding: 30px 36px 32px; background: #fff; color: var(--ink);
          font-family: 'IBM Plex Sans', sans-serif; }
h1 { font-size: 23px; font-weight: 700; letter-spacing: -0.01em; }
.lede { font-size: 15px; color: var(--muted); margin: 6px 0 26px; }
svg { display: block; overflow: visible; font-family: 'IBM Plex Sans', sans-serif; }
svg text { fill: var(--ink); }
.h { font-size: 16px; font-weight: 700; }
.hs { font-size: 13px; fill: var(--muted); }
.bt { font-size: 14px; font-weight: 600; }
.bs { font-size: 12.5px; fill: var(--muted); }
.m { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; fill: var(--muted); }
.mc { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; fill: var(--coral); font-weight: 500; }
.note { font-size: 14px; font-weight: 600; }
.box { fill: #fff; stroke: var(--rule); }
.rule { stroke: var(--rule); }
.grid { stroke: var(--grid); }
.navy { fill: var(--navy); } .coral { fill: var(--coral); }
.edge { fill: none; stroke: var(--dash); stroke-width: 1.4; stroke-linejoin: round; }
.dashed { stroke-dasharray: 4 3; }
.zone-model { fill: var(--coral-tint); stroke: #f0d9d4; }
.zone-code { fill: var(--navy-tint); stroke: #dce3ed; }
.shots { display: grid; }
.shot img { width: 100%; display: block; border: 1px solid var(--rule); }
.cap { margin-top: 8px; font-size: 14px; font-weight: 600; }
.cap span { font-weight: 400; color: var(--muted); }
.mono { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--muted); font-weight: 400; }
.cap .mono { display: block; margin-top: 2px; white-space: pre; }
.cap .mono span { color: var(--muted); }
"""


def render(name: str, title: str, lede: str, body: str, folder: Path | None = None) -> None:
    """Screenshot one figure page to docs/media/<name> (.png lossless, .webp lossy). Images in `body` are relative
    to `folder`."""
    from PIL import Image
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(folder or tmp) / f"_{Path(name).stem}.html"
        page_path.write_text(f'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="{FONTS}">'
                             f'<style>{CSS}</style><div id="figure"><h1>{escape(title)}</h1>'
                             f'<p class="lede">{escape(lede)}</p>{body}</div>')
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(device_scale_factor=2, viewport={"width": 960, "height": 600})
            page.goto(page_path.as_uri())
            page.evaluate("document.fonts.ready")
            page.wait_for_load_state("networkidle")
            shot = Path(tmp) / "shot.png"
            page.locator("#figure").screenshot(path=shot)
            browser.close()
        page_path.unlink()
        out = MEDIA / name
        if out.suffix == ".webp":
            Image.open(shot).convert("RGB").save(out, quality=86, method=6)
        else:
            Image.open(shot).convert("RGB").quantize(256, dither=Image.Dither.NONE).save(out, optimize=True)
    print(out)


# ---- SVG pieces -----------------------------------------------------------------------------------------------

ARROW = ('<defs><marker id="a" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" '
         'orient="auto-start-reverse"><path d="M1,1.5 L9,5 L1,8.5" fill="none" stroke="#8b95a3" stroke-width="1.6" '
         'stroke-linecap="round" stroke-linejoin="round"/></marker></defs>')


def svg(h: int, *parts: str) -> str:
    return f'<svg width="{WIDTH}" height="{h}" viewBox="0 0 {WIDTH} {h}">{ARROW}{"".join(parts)}</svg>'


def text(x, y, s, cls="bs", anchor="start") -> str:
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{escape(s)}</text>'


def label(x, y, s) -> str:
    """A mono annotation centred above a line."""
    return text(x, y, s, "m", "middle")


def box(x, y, w, h, title, sub="", model=False, mono=False) -> str:
    """A step: white, a thin rule, a 3px accent on the left (coral: a model; navy: code)."""
    top = y + h / 2 + (-3 if sub else 5)
    return (f'<rect class="box" x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/>'
            f'<rect class="{"coral" if model else "navy"}" x="{x}" y="{y}" width="3" height="{h}" rx="1"/>'
            + text(x + 16, top, title, "bt") + (text(x + 16, top + 18, sub, "m" if mono else "bs") if sub else ""))


def edge(d: str, dashed=False) -> str:
    return f'<path class="edge{" dashed" if dashed else ""}" d="{d}" marker-end="url(#a)"/>'


def header(x, title, sub, w=None, rule=True, n=None) -> str:
    number = f'<tspan fill="#8b95a3">{n}</tspan><tspan dx="9">' if n else "<tspan>"
    out = f'<text class="h" x="{x}" y="17">{number}{escape(title)}</tspan></text>' + text(x, 37, sub, "hs")
    return out + (f'<line class="rule" x1="{x}" x2="{x + w}" y1="52.5" y2="52.5"/>' if rule else "")


def legend(y) -> str:
    return (f'<rect class="coral" x="0" y="{y - 11}" width="3" height="14" rx="1"/>'
            + text(12, y, "model-driven step")
            + f'<rect class="navy" x="152" y="{y - 11}" width="3" height="14" rx="1"/>'
            + text(164, y, "deterministic code")
            + text(318, y, "cheap model", "mc") + text(404, y, "which model tier runs it"))


# ---- diagrams -------------------------------------------------------------------------------------------------

def pipeline() -> None:
    """Three columns, one per stage, read top to bottom; a loop on the right returns to the step it repeats."""
    stages = [
        ("Design", "an image the site can reproduce",
         [("Plan", "least-used style + layout", None), ("Draw", "Codex image tool", "image model"),
          ("Review", "accept, revise or redraw", "reviewer")], ["revise or", "redraw"]),
        ("Build", "agents build, the gate decides",
         [("Scaffold", "already passes the checker", None), ("Agent turn", "render + check tools", "cheap model"),
          ("Gate", "oracle at 5 viewports", None)], ["retry on", "stronger", "model"]),
        ("Polish", "raise fidelity, never break it",
         [("Review", "fidelity 1-5, what differs", "other model"),
          ("Polish", "works the differences", "strongest model"),
          ("Keep or roll back", "gate passes, score held", None)], None),
    ]
    parts, w, bw, bh, top, step = [], 280, 192, 58, 86, 100
    for k, (name, sub, steps, loop) in enumerate(stages):
        x = k * (w + 24)
        parts.append(header(x, name, sub, w, n=k + 1))
        if k:
            parts.append(edge(f"M{x - 22},12 H{x - 9}"))
        for i, (title, note, model) in enumerate(steps):
            y = top + i * step
            parts.append(box(x, y, bw, bh, title, note, model=bool(model)))
            if model:
                parts.append(text(x + bw, y - 8, model, "mc", "end"))
            if i:
                parts.append(edge(f"M{x + 34},{y - step + bh + 1} V{y - 2}"))
        if loop:
            a, b, lx = top + 2 * step + bh / 2, top + step + bh / 2, x + bw + 22
            parts.append(edge(f"M{x + bw + 1},{a} H{lx} V{b} H{x + bw + 3}"))
            mid = (a + b) / 2 - 7 * (len(loop) - 1) + 4
            parts += [text(lx + 9, mid + 14 * j, line, "m") for j, line in enumerate(loop)]
    parts.append(legend(top + 2 * step + bh + 46))
    render("pipeline.png", "Three stages, each with its own loop",
           "Models do the creative work. Deterministic code decides what moves on.",
           svg(top + 2 * step + bh + 54, *parts))


def gate() -> None:
    """The checks in order, then the five training viewports the oracle plays at, drawn to scale."""
    checks = [("Files", "the expected files exist", False), ("Syntax", "node --check", True),
              ("Lint", "megawob.lint", True), ("Oracle", "megawob.check", True)]
    parts, bw, gap = [], 198, 32
    for i, (title, sub, mono) in enumerate(checks):
        x = i * (bw + gap)
        parts.append(box(x, 0, bw, 60, title, sub, mono=mono))
        if i:
            parts.append(edge(f"M{x - gap + 1},30 H{x - 3}"))
    sizes = [(1024, 640), (1280, 720), (1366, 768), (1440, 900), (1536, 864)]
    quick = {(1024, 640), (1366, 768), (1536, 864)}
    scale, base, vgap = 0.12, 254, 24
    widths = [w * scale for w, _ in sizes]
    x = (WIDTH - sum(widths) - vgap * 4) / 2
    centers = []
    parts.append(f'<line class="grid" x1="0" x2="{WIDTH}" y1="{base + 0.5}" y2="{base + 0.5}"/>')
    for (w, h), sw in zip(sizes, widths, strict=True):
        sh, top = h * scale, base - h * scale
        parts += [f'<rect class="box" x="{x}" y="{top}" width="{sw}" height="{sh}" rx="3"/>',
                  f'<rect fill="#e9edf2" x="{x + 0.5}" y="{top + 0.5}" width="{sw - 1}" height="9" rx="2.5"/>',
                  f'<rect fill="#dce3ed" x="{x + sw * 0.2}" y="{top + 19}" width="{sw * 0.6}" height="{sh - 29}" '
                  'rx="2"/>',
                  text(x + sw / 2, base + 20, f"{w}×{h}", "m", "middle")]
        if (w, h) in quick:
            parts.append(f'<circle class="coral" cx="{x + 7}" cy="{top + 5}" r="2.6"/>')
        centers.append(x + sw / 2)
        x += sw + vgap
    oracle, bracket = 3 * (bw + gap) + bw / 2, 118
    parts += [f'<path class="edge dashed" d="M{oracle},61 V{bracket}"/>',
              f'<path class="edge dashed" d="M{centers[0]},{bracket + 16} V{bracket} H{centers[-1]} '
              f'V{bracket + 16}"/>',
              *[f'<path class="edge dashed" d="M{c},{bracket} V{bracket + 16}"/>' for c in centers[1:-1]],
              text(0, 98, "Every seed and random setting, at all five training viewports", "note"),
              text(0, base + 64, "Any failure goes back to the agent, word for word, as a fix round.", "note"),
              f'<circle class="coral" cx="{WIDTH - 226}" cy="{base + 59.5}" r="2.6"/>',
              text(WIDTH - 216, base + 64, "also in the agent's own quick check()")]
    render("gate.png", "The gate every site must pass",
           "Deterministic checks, run outside the agent's reach after every turn.", svg(base + 72, *parts))


def architecture() -> None:
    """Three zones side by side: the harness that decides, the agent's sandbox, and the tools server."""
    zones = [(0, 228, "Harness", "decides after every turn", "code"),
             (312, 264, "Agent sandbox", "writes only the site folder", "model"),
             (648, 240, "Tools server", "outside the sandbox, over MCP", "code")]
    parts, top, bottom = [], 64, 312
    for x, w, title, sub, kind in zones:
        parts += [header(x, title, sub, rule=False),
                  f'<rect class="zone-{kind}" x="{x}" y="{top}" width="{w}" height="{bottom - top}" rx="6"/>']
    hx, hw, sx, sw, tx, tw = 16, 196, 328, 232, 664, 208
    parts += [box(hx, 84, hw, 56, "Fix rounds", "to a stronger model"),
              box(hx, 168, hw, 56, "Full gate", "decides acceptance"),
              box(hx, 236, hw, 56, "Reviewer", "other model, no tools", model=True),
              box(sx, 84, sw, 56, "Coding agent", "Codex or Claude", model=True),
              f'<rect class="box" x="{sx}" y="168" width="{sw}" height="124" rx="3"/>',
              f'<rect class="navy" x="{sx}" y="168" width="3" height="124" rx="1"/>',
              text(sx + 16, 194, "Site files", "bt"), text(sx + 16, 213, "html/sites/design-…/", "m"),
              text(sx + 16, 232, "site.js, site.css, manifest", "bs"),
              box(tx, 84, tw, 56, "render()", "headless Chromium"),
              box(tx, 168, tw, 56, "check()", "lint + oracle, 3 viewports"),
              text(tx, 262, "reads the site files,", "m"), text(tx, 279, "never writes them", "m"),
              edge(f"M{sx + 40},141 V166"), text(sx + 50, 158, "edits", "m"),
              edge(f"M{hx + hw + 1},112 H{sx - 3}"), label(270, 106, "failures"),
              edge(f"M{hx + hw + 1},196 H{sx - 3}"), label(270, 190, "checks"),
              edge(f"M{sx - 1},264 H{hx + hw + 3}", dashed=True), label(270, 258, "screenshots"),
              edge(f"M{sx + sw + 1},104 H{tx - 3}"), label(612, 98, "MCP"),
              edge(f"M{sx + sw + 1},122 H594 V196 H{tx - 3}")]
    render("architecture.png", "Who writes, and who decides",
           "The agent can change only its site folder. Everything that judges it runs outside the sandbox.",
           svg(bottom + 2, *parts))


def diagrams() -> None:
    pipeline()
    gate()
    architecture()


# ---- screenshots of built sites (need a megawob checkout) -------------------------------------------------------

FOUR = [("design-use-email-archive-01", "Material paper", "2014", "tabs above a centred card"),
        ("design-use-email-archive-02", "Precision dark", "2026", "left rail, list in a sub-window"),
        ("design-use-email-archive-03", "Amiga Workbench", "1990", "bottom dock, notes on the right"),
        ("design-use-email-archive-04", "BeOS", "1999", "title bar only, lower inspector")]
# (caption, the setting it shows) per MODES entry; finishByMessage is 0 unless shown
MODE_CAPTIONS = [("Header", "instructionDisplay=header"), ("Panel", "instructionDisplay=panel"),
                 ("Toast", "instructionDisplay=toast"), ("Hidden, finish by message", "finishByMessage=1")]


def shoot(site: str, size=(1536, 864), params: dict | None = None):
    from megawob import catalog
    from PIL import Image

    from megawob_harness.render import Renderer

    desc = json.loads((catalog.HTML / "sites" / site / "manifest.json").read_text())["description"]
    task, scenario = desc.split("/")[:2]
    with tempfile.TemporaryDirectory() as tmp:
        blank = Path(tmp) / "blank.png"  # the design image, unused by a plain capture
        Image.new("RGB", size, "white").save(blank)
        with Renderer(site, task, scenario, blank) as r:
            return r.capture(0, size, params=params)[0]


def collage_sites(n=20) -> list[tuple[str, str, str]]:
    """(site, task, year) of n built sites, one per task, the years rotating through four eras."""
    from megawob import catalog

    eras, by_task = [(1980, 1995), (2006, 2015), (1996, 2005), (2016, 2026)], {}
    for manifest in sorted((catalog.HTML / "sites").glob("design-*/manifest.json")):
        m = re.match(r"([\w-]+)/[\w-]+/\d+-(\d{4})-", json.loads(manifest.read_text())["description"])
        if m:  # task/scenario/NN-<year>-<style>-...; a few older sites describe themselves differently
            by_task.setdefault(m[1], []).append((manifest.parent.name, int(m[2])))
    tasks = sorted(by_task)
    picked = []
    for k, task in enumerate(tasks[i * len(tasks) // n] for i in range(n)):
        lo, hi = eras[k % len(eras)]
        site, year = next(((s, y) for s, y in by_task[task] if lo <= y <= hi), by_task[task][0])
        picked.append((site, task, str(year)))
    return picked


def grid(shots: list[tuple[str, str]], cols: int, gap="22px 18px") -> str:
    cells = "".join(f'<div class="shot"><img src="{src}"><div class="cap">{cap}</div></div>' for src, cap in shots)
    return f'<div class="shots" style="grid-template-columns: repeat({cols}, minmax(0, 1fr)); gap: {gap}">{cells}</div>'


def shots() -> None:
    from megawob_harness.render import MODES, MODES_SIZE

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)

        def save(img, name: str, width: int) -> str:
            img.resize((width, round(img.height * width / img.width))).save(folder / name, quality=92)
            return name

        sites = collage_sites()
        cells = [(save(shoot(site), f"c{i}.jpg", 340),
                  f'<span class="mono" style="color:#1d2b3a">{escape(task)} <span>{year}</span></span>')
                 for i, (site, task, year) in enumerate(sites)]
        render("collage.webp", "A sample of the 2,942 shipped sites",
               f"One site for each of {len(sites)} tasks, in styles from 1984 to 2026, each passing the same gate.",
               grid(cells, 5, "16px 12px"), folder)

        cells = [(save(shoot(site), f"d{i}.jpg", 860), f"{style} <span>· {year} · {layout}</span>")
                 for i, (site, style, year, layout) in enumerate(FOUR)]
        render("one-task-four-designs.webp", "Same task, same seed, four structures",
               "use-email / archive at the same seed, built from four different design images.", grid(cells, 2),
               folder)

        site = FOUR[0][0]
        cells = []
        for i, ((_, params), (caption, setting)) in enumerate(zip(MODES, MODE_CAPTIONS, strict=True)):
            cells.append((save(shoot(site, MODES_SIZE, params), f"m{i}.png", 440),
                          f'{caption}<span class="mono">{escape(setting)}</span>'))
        render("render-modes.webp", "One frame, four instruction settings",
               'What the build agent\'s render(view="modes") shows it, at the tightest training viewport (1024×640).',
               grid(cells, 4, "0 14px"), folder)


if __name__ == "__main__":
    MEDIA.mkdir(exist_ok=True)
    {"diagrams": diagrams, "shots": shots}[sys.argv[1] if len(sys.argv) > 1 else "diagrams"]()
