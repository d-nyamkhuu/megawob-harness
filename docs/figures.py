"""Regenerate the README figures in docs/media from a megawob checkout.

    cd path/to/megawob && python path/to/megawob-harness/docs/figures.py

- one-task-four-designs.webp: four built sites of one task and scenario, same seed, side by side.
- render-modes.webp: what the build agent's `render(view="modes")` tool returns.
- sites-by-era-{light,dark}.svg: the design-* sites in the checkout by the decade of their style profile.
- trust-boundaries-{light,dark}.svg: what the agent can write, and what judges it from outside its reach.
- stop-gate-minutes-{light,dark}.svg: the Stop-gate experiment (experiments/stop-gate/summary.json), per design.
"""
from __future__ import annotations

import collections
import json
import re
import tempfile
from pathlib import Path

from megawob import catalog
from PIL import Image, ImageDraw, ImageFont

from megawob_harness.render import Renderer

MEDIA = Path(__file__).resolve().parent / "media"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FOUR = [("design-use-email-archive-01", "2014 · Material paper", "tabs above a centred card"),
        ("design-use-email-archive-02", "2026 · Precision dark", "left rail, list in a sub-window"),
        ("design-use-email-archive-03", "1990 · Amiga Workbench", "bottom dock, notes on the right"),
        ("design-use-email-archive-04", "1999 · BeOS", "title bar only, lower inspector")]


def shoot(site: str, view: str | None = None) -> Image.Image:
    desc = json.loads((catalog.HTML / "sites" / site / "manifest.json").read_text())["description"]
    task, scenario = desc.split("/")[:2]
    with tempfile.TemporaryDirectory() as tmp:
        blank = Path(tmp) / "blank.png"  # the design image, unused by these views
        Image.new("RGB", (1536, 864), "white").save(blank)
        with Renderer(site, task, scenario, blank) as r:
            return r.compare(0, view)[0] if view else r.capture(0, (1536, 864))[0]


def four_designs() -> None:
    w, h, pad, cap = 760, 428, 24, 64
    out = Image.new("RGB", (2 * w + 3 * pad, 2 * (h + cap) + 3 * pad), "#f6f8fa")
    draw = ImageDraw.Draw(out)
    title, sub = ImageFont.truetype(BOLD, 22), ImageFont.truetype(FONT, 18)
    for k, (site, style, layout) in enumerate(FOUR):
        x, y = pad + (k % 2) * (w + pad), pad + (k // 2) * (h + cap + pad)
        draw.rounded_rectangle((x - 1, y - 1, x + w, y + h + cap), 10, fill="white", outline="#d0d7de")
        out.paste(shoot(site).resize((w - 2, h), Image.LANCZOS), (x, y))
        draw.text((x + 16, y + h + 10), style, font=title, fill="#1f2328")
        draw.text((x + 16, y + h + 38), layout, font=sub, fill="#59636e")
    out.save(MEDIA / "one-task-four-designs.webp", quality=82)


def render_modes() -> None:
    img = shoot("design-use-email-archive-01", "modes")
    img.resize((1400, round(img.height * 1400 / img.width)), Image.LANCZOS).save(MEDIA / "render-modes.webp",
                                                                                   quality=82)


def sites_by_era() -> None:
    decades: collections.Counter = collections.Counter()
    for manifest in (catalog.HTML / "sites").glob("design-*/manifest.json"):
        if m := re.match(r"[\w-]+/[\w-]+/\d+-(\d{4})-", json.loads(manifest.read_text()).get("description", "")):
            decades[int(m[1]) // 10 * 10] += 1
    bars = sorted(decades.items())
    themes = {"light": {"bar": "#2a78d6", "ink": "#1f2328", "muted": "#59636e", "rule": "#d0d7de"},
              "dark": {"bar": "#3987e5", "ink": "#f0efec", "muted": "#c3c2b7", "rule": "#3d444d"}}
    W, H, left, base, top, bw, gap = 720, 280, 24, 236, 40, 96, 40
    x0 = (W - len(bars) * bw - (len(bars) - 1) * gap) // 2  # the bars centred
    peak = max(n for _, n in bars)
    for name, t in themes.items():
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
                 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
                 f'<line x1="{left}" y1="{base}" x2="{W - left}" y2="{base}" stroke="{t["rule"]}" stroke-width="1"/>']
        for i, (decade, n) in enumerate(bars):
            x, bh = x0 + i * (bw + gap), round((base - top) * n / peak)
            y, r = base - bh, 4
            top_edge = f"V{y + r} Q{x},{y} {x + r},{y} H{x + bw - r} Q{x + bw},{y} {x + bw},{y + r}"  # rounded ends
            parts += [f'<path d="M{x},{base} {top_edge} V{base} Z" fill="{t["bar"]}">'
                      f'<title>{decade}s: {n:,} sites</title></path>',
                      f'<text x="{x + bw / 2}" y="{y - 8}" text-anchor="middle" font-size="15" font-weight="600" '
                      f'fill="{t["ink"]}">{n:,}</text>',
                      f'<text x="{x + bw / 2}" y="{base + 22}" text-anchor="middle" font-size="14" '
                      f'fill="{t["muted"]}">{decade}s</text>']
        (MEDIA / f"sites-by-era-{name}.svg").write_text("\n".join([*parts, "</svg>"]) + "\n")
    print(f"{sum(decades.values())} sites by decade: {bars}")


TRUST_THEMES = {
    "light": {"ink": "#1f2328", "muted": "#59636e", "node": "#ffffff", "edge": "#59636e", "node_line": "#8c959f",
              "agent": ("#fff1e5", "#fb8f44"), "trusted": ("#ddf4ff", "#54aeff")},
    "dark": {"ink": "#e6edf3", "muted": "#9198a1", "node": "#161b22", "edge": "#9198a1", "node_line": "#6e7681",
             "agent": ("#2d1b0e", "#bd561d"), "trusted": ("#0c2d4a", "#1f6feb")},
}
#: zones: (x, y, w, h, title, note, kind); nodes: name -> (x, y, w, h, label, sublabel)
ZONES = [(20, 20, 380, 250, "Agent sandbox", "writes only the site folder", "agent"),
         (480, 20, 380, 250, "Tools server", "outside the sandbox (MCP)", "trusted"),
         (20, 310, 840, 120, "Harness", "after every turn, whatever the agent reports", "trusted")]
NODES = {"agent": (60, 84, 240, 58, "Coding agent", "Codex or Claude"),
         "site": (60, 186, 240, 52, "site files", "html/sites/design-…/"),
         "render": (530, 74, 300, 56, "render()", "headless Chromium, design image beside the page"),
         "check": (530, 164, 300, 56, "check()", "lint + oracle at 3 viewports"),
         "gate": (50, 352, 170, 56, "Full gate", "5 viewports, more seeds"),
         "review": (240, 352, 170, 56, "Reviewer", "no tools, other model"),
         "fix": (450, 352, 180, 56, "Fix rounds", "failures to a stronger model"),
         "record": (650, 352, 190, 56, "Run record", "every turn, gate, review")}
#: edges: (from point, to point, label, dashed)
EDGES = [((300, 104), (530, 102), "MCP", False), ((300, 124), (530, 192), "MCP", False),
         ((180, 142), (180, 186), "edits", False),
         ((135, 352), (135, 238), "decides", False), ((325, 352), (260, 238), "screenshots", True)]


def trust_boundaries() -> None:
    for name, t in TRUST_THEMES.items():
        parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 880 450" width="880" height="450" '
                 'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
                 f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                 f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{t["edge"]}"/></marker></defs>']
        for x, y, w, h, title, note, kind in ZONES:
            fill, line = t[kind]
            parts += [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{line}" '
                      'stroke-width="1.5"/>',
                      f'<text x="{x + 16}" y="{y + 26}" font-size="15" font-weight="600" fill="{t["ink"]}">{title}'
                      f'<tspan font-weight="400" fill="{t["muted"]}">  ·  {note}</tspan></text>']
        for x, y, w, h, label, sub in NODES.values():
            parts += [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{t["node"]}" '
                      f'stroke="{t["node_line"]}"/>',
                      f'<text x="{x + w / 2}" y="{y + h / 2 - 3}" text-anchor="middle" font-size="14" '
                      f'font-weight="600" fill="{t["ink"]}">{label}</text>',
                      f'<text x="{x + w / 2}" y="{y + h / 2 + 15}" text-anchor="middle" font-size="12" '
                      f'fill="{t["muted"]}">{sub}</text>']
        for (x1, y1), (x2, y2), label, dashed in EDGES:
            dash = ' stroke-dasharray="5 4"' if dashed else ""
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{t["edge"]}" stroke-width="1.5"'
                         f'{dash} marker-end="url(#arrow)"/>')
            if label:
                parts.append(f'<text x="{(x1 + x2) / 2 + 8}" y="{(y1 + y2) / 2 - 6}" font-size="12" '
                             f'fill="{t["muted"]}">{label}</text>')
        (MEDIA / f"trust-boundaries-{name}.svg").write_text("\n".join([*parts, "</svg>"]) + "\n")


STOP_GATE = Path(__file__).resolve().parent.parent / "experiments" / "stop-gate" / "summary.json"
LABELS = {"set-slider": "Slider", "fill-form": "Form", "use-email": "Email app"}
PAIR = {"light": {"stop-gate": "#2a78d6", "no-stop-gate": "#eb6834", "ink": "#1f2328", "muted": "#59636e",
                  "rule": "#d0d7de", "surface": "#ffffff"},
        "dark": {"stop-gate": "#3987e5", "no-stop-gate": "#d95926", "ink": "#f0efec", "muted": "#c3c2b7",
                 "rule": "#3d444d", "surface": "#0d1117"}}


def stop_gate_minutes() -> None:
    """A dumbbell per design: minutes to an accepted site with and without the Stop gate, and the fix rounds."""
    data = json.loads(STOP_GATE.read_text())
    sites = sorted({r["site"] for r in data})
    by = {(r["site"], r["condition"]): r for r in data}
    W, top, row, left, right, xmax = 760, 70, 52, 120, 200, 25
    H = top + row * len(sites) + 40

    def x(minutes: float) -> float:
        return left + (W - left - right) * minutes / xmax

    for theme, t in PAIR.items():
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
               'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">']
        for i, (condition, label) in enumerate([("stop-gate", "with Stop hook"), ("no-stop-gate", "without")]):
            lx = left + i * 150
            out += [f'<circle cx="{lx + 6}" cy="22" r="6" fill="{t[condition]}"/>',
                    f'<text x="{lx + 18}" y="27" font-size="14" fill="{t["ink"]}">{label}</text>']
        out.append(f'<text x="{W - right + 24}" y="56" font-size="13" fill="{t["muted"]}">fix rounds</text>')
        for m in range(0, xmax + 1, 5):
            out += [f'<line x1="{x(m)}" y1="{top - 14}" x2="{x(m)}" y2="{H - 34}" stroke="{t["rule"]}"/>',
                    f'<text x="{x(m)}" y="{H - 14}" text-anchor="middle" font-size="12" fill="{t["muted"]}">'
                    f'{m}{" min" if m == xmax else ""}</text>']
        for k, site in enumerate(sites):
            y = top + row * k + row / 2 - 10
            on, off = by[(site, "stop-gate")], by[(site, "no-stop-gate")]
            task = next(name for slug, name in LABELS.items() if site.startswith(f"design-{slug}-"))
            out += [f'<text x="{left - 16}" y="{y + 5}" text-anchor="end" font-size="14" fill="{t["ink"]}">{task}'
                    '</text>',
                    f'<line x1="{x(on["minutes"])}" y1="{y}" x2="{x(off["minutes"])}" y2="{y}" '
                    f'stroke="{t["muted"]}" stroke-width="2"/>']
            for r, condition in ((on, "stop-gate"), (off, "no-stop-gate")):
                out += [f'<circle cx="{x(r["minutes"])}" cy="{y}" r="7" fill="{t[condition]}" '
                        f'stroke="{t["surface"]}" stroke-width="2"><title>{task}, {condition}: {r["minutes"]} min, '
                        f'{r["fix_rounds"]} fix rounds</title></circle>',
                        f'<text x="{x(r["minutes"])}" y="{y - 13 if condition == "stop-gate" else y + 24}" '
                        f'text-anchor="middle" font-size="12" fill="{t["ink"]}">{r["minutes"]}</text>']
            out.append(f'<text x="{W - right + 24}" y="{y + 5}" font-size="14" fill="{t["ink"]}">'
                       f'{on["fix_rounds"]} <tspan fill="{t["muted"]}">vs</tspan> {off["fix_rounds"]}</text>')
        (MEDIA / f"stop-gate-minutes-{theme}.svg").write_text("\n".join([*out, "</svg>"]) + "\n")


if __name__ == "__main__":
    MEDIA.mkdir(exist_ok=True)
    four_designs()
    render_modes()
    sites_by_era()
    trust_boundaries()
    stop_gate_minutes()
