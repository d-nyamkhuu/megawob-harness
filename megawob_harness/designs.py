"""Design images: what a new design-* site is built from.

A design is one base screen, 1536x864 (the largest training viewport), with no task note and no end control: the
hidden-instruction, finish-by-message episode, so it fits every randomization-parameter setting; the build agent
adds the episode frame. Each design gets
- a style profile (styles.json): the one its task's designs have used least, never one its scenario has;
- a window arrangement (arrangements.json, one option per axis): a combination its task has not used, the
  options balanced within the scenario, then the task;
- a task-area arrangement from the task's capability card, taken in turn;
- a real goal of the scenario (sampled from the pages, as megawob.describe does), which the screen depicts.

The capability card (cards/<task>.json) says what the task's code renders at the start of each scenario, so
the image never shows a state the site cannot reproduce. `card` builds it from the task's logic.js header.

An image is checked (16:9, not blank), then reviewed against the brief by a separate turn with a checklist and a
JSON verdict: accept, revise (an edit of the image with one correction) or reject (a fresh image), up to
`attempts` images; the last attempt is always drawn fresh.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import random
import re
from datetime import datetime
from pathlib import Path

from megawob import catalog, describe, driver
from PIL import Image, ImageStat

from .runtime import AgentError, Runtime

HERE = Path(__file__).resolve().parent
CARDS = HERE / "cards"
PROFILES = {p["slug"]: p for p in json.loads((HERE / "styles.json").read_text())["profiles"]}
AXES = json.loads((HERE / "arrangements.json").read_text())["axes"]
AXIS_LABELS = {"nav": "Navigation", "place": "Task area", "aux": "Secondary content (required, keep it visible)"}
COMBOS = [dict(zip(AXES, c, strict=True)) for c in itertools.product(*(list(o) for o in AXES.values()))]
SIZE = (1536, 864)
SCENE_SHARE = 0.3  # share of designs drawn in the task's documented mid-task state, where it has one

CARD_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["identity", "scenarios"],
    "properties": {
        "identity": {"type": "string"},
        "scenarios": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "screen", "scene", "never", "arrangements"],
            "properties": {"name": {"type": "string"}, "screen": {"type": "string"}, "scene": {"type": "string"},
                           "never": {"type": "array", "items": {"type": "string"}},
                           "arrangements": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["text", "traits"],
                               "properties": {"text": {"type": "string"}, "traits": {"type": "string"}}}}}}},
    },
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "score", "blocking", "minor", "correction"],
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "revise", "reject"]},
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "blocking": {"type": "array", "items": {"type": "string"}},
        "minor": {"type": "array", "items": {"type": "string"}},
        "correction": {"type": "string"},
    },
}


def template(name: str) -> str:
    return (HERE / "prompts" / f"{name}.txt").read_text()


def squeeze(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip() + "\n"


def seeded(*parts) -> random.Random:
    return random.Random(hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest())


def scenarios(task: str) -> list[str]:
    return catalog.scenarios_index()["tasks"].get(task, {}).get("scenarios", [])


def start_modes(task: str) -> list[str]:
    return catalog.scenarios_index()["tasks"].get(task, {}).get("startModes", [])


def task_doc(task: str) -> str:
    """The task's developer documentation: the header comment of its logic.js."""
    text = (catalog.HTML / "tasks" / task / "logic.js").read_text()
    m = re.match(r"\s*/\*(.*?)\*/", text, re.S)
    return "\n".join(re.sub(r"^\s*\* ?", "", line) for line in (m[1] if m else "").splitlines()).strip()


def catalog_row(task: str, examples: int = 12) -> dict:
    """megawob.describe's row: description, inputs and `examples` real goals per scenario (from the pages)."""
    with driver.browser() as browser:
        page = browser.new_page(viewport={"width": driver.VIEWPORT[0], "height": driver.VIEWPORT[1]})
        return describe.describe(page, task, examples)


# ---- capability cards ----------------------------------------------------------------------------------------

def card_path(task: str) -> Path:
    return CARDS / f"{task}.json"


def load_card(task: str) -> dict:
    path = card_path(task)
    if not path.is_file():
        raise SystemExit(f"{task}: no capability card yet; run `megawob-harness card {task}`")
    return json.loads(path.read_text())


def make_card(agent: Runtime, task: str) -> Path:
    """Build cards/<task>.json from the task's logic.js header, start modes and sample goals. Scenario hints
    (`hint`) of an existing card are kept: they are hand-written guidance."""
    row = catalog_row(task, 2)
    listing = "\n".join(f"- {s}: e.g. " + " | ".join(goals) for s, goals in row["scenarios"].items())
    prompt = template("card").format(task=task, description=row["description"], doc=task_doc(task)[:40000],
                                     start_modes=", ".join(start_modes(task)), scenarios=listing)
    card = agent.ask(prompt, CARD_SCHEMA)
    old = ({s["name"]: s for s in json.loads(card_path(task).read_text())["scenarios"]}
           if card_path(task).is_file() else {})
    for s in card["scenarios"]:
        s["hint"] = old.get(s["name"], {}).get("hint", "")
    missing = set(scenarios(task)) - {s["name"] for s in card["scenarios"]}
    if missing:
        raise AgentError(f"{task}: the card misses scenarios {sorted(missing)}")
    driver.write_json(card_path(task), {"task": task, **card})
    return card_path(task)


# ---- planning ------------------------------------------------------------------------------------------------

def arrangement_slug(combo: dict) -> str:
    return "-".join(combo[a] for a in AXES)


def used(task: str, out: Path) -> list[dict]:
    """The designs of `task` so far: every design-<task>-* site (its manifest description names the profile and
    arrangement, e.g. "use-email/archive/01-2014-material-paper-tabs-center-stats") and every design planned in
    `out`. Each as {scenario, index, profile, combo}."""
    found = {}
    by_slug = {arrangement_slug(c): c for c in COMBOS}
    for manifest in sorted((catalog.HTML / "sites").glob(f"design-{task}-*/manifest.json")):
        desc = json.loads(manifest.read_text()).get("description", "")
        m = re.match(rf"{re.escape(task)}/([\w-]+)/(\d+)-\d{{4}}-(.+)$", desc)
        if not m:
            continue
        rest = m[3]
        profile = next((p for p in sorted(PROFILES, key=len, reverse=True) if rest.startswith(p + "-")), None)
        combo = by_slug.get(rest[len(profile) + 1:]) if profile else None
        found[(m[1], int(m[2]))] = {"scenario": m[1], "index": int(m[2]), "profile": profile, "combo": combo}
    for path in sorted((out / "designs" / task).glob("*/*/design.json")):
        d = json.loads(path.read_text())
        found[(d["scenario"], d["index"])] = {"scenario": d["scenario"], "index": d["index"],
                                              "profile": d["style"]["slug"], "combo": d["arrangement"]}
    for site in (catalog.HTML / "sites").glob(f"design-{task}-*"):  # sites without a parseable description
        if m := re.fullmatch(rf"design-{re.escape(task)}-(.+)-(\d+)", site.name):
            found.setdefault((m[1], int(m[2])), {"scenario": m[1], "index": int(m[2]), "profile": None, "combo": None})
    return list(found.values())


def plan(task: str, scenario: str, out: Path, profile: str | None = None) -> Path:
    """Allocate the next design of task/scenario and write <out>/designs/<task>/<scenario>/<NN>/design.json."""
    if scenario not in scenarios(task):
        raise SystemExit(f"{task} has no scenario {scenario!r}; it has {', '.join(scenarios(task))}")
    if profile and profile not in PROFILES:
        raise SystemExit(f"unknown style {profile!r}; see styles.json")
    card = load_card(task)
    caps = next((s for s in card["scenarios"] if s["name"] == scenario), None)
    if caps is None:
        raise SystemExit(f"{task}: the capability card has no {scenario!r}; "
                         f"rebuild it with `megawob-harness card {task}`")
    have = used(task, out)
    index = max((d["index"] for d in have if d["scenario"] == scenario), default=0) + 1
    rng = seeded(task, scenario, index)
    # Style: never one the scenario has, the least used in the task.
    in_scenario = {d["profile"] for d in have if d["scenario"] == scenario}
    use = {p: sum(d["profile"] == p for d in have) for p in PROFILES}
    profile = profile or min((p for p in PROFILES if p not in in_scenario) or PROFILES,
                             key=lambda p: (use[p], rng.random()))
    # Window arrangement: a combination the task has not used, options balanced in the scenario, then the task.
    combos_used = {arrangement_slug(d["combo"]) for d in have if d["combo"]}

    def load(c, ds):
        return max(sum(d["combo"] is not None and d["combo"][a] == c[a] for d in ds) for a in AXES)
    scen = [d for d in have if d["scenario"] == scenario]
    combo = min([c for c in COMBOS if arrangement_slug(c) not in combos_used] or COMBOS,
                key=lambda c: (load(c, scen), load(c, have), rng.random()))
    options = caps["arrangements"]
    arrange = options[(index - 1) % len(options)] if options else None
    row = catalog_row(task, 12)
    goals = row["scenarios"].get(scenario) or [""]
    scene = "scene" in start_modes(task) and bool(caps["scene"]) and rng.random() < SCENE_SHARE
    p = PROFILES[profile]
    design = {
        "id": f"{task}/{scenario}/{index:02d}-{p['era']}-{profile}-{arrangement_slug(combo)}",
        "site": f"design-{task}-{scenario}-{index:02d}", "task": task, "scenario": scenario, "index": index,
        "platform": p["platform"], "era": p["era"], "style": {"slug": profile, "desc": p["desc"]},
        "arrangement": combo, "arrange": arrange, "description": row["description"],
        "inputs": row["scenario_inputs"].get(scenario) or row["inputs"], "sample": goals[(index - 1) % len(goals)],
        "identity": card["identity"], "screen": caps["scene"] if scene else caps["screen"],
        "start": "scene" if scene else "off", "never": caps["never"], "hint": caps.get("hint", ""),
        "size": list(SIZE), "planned": datetime.now().isoformat(timespec="seconds"),
    }
    path = out / "designs" / task / scenario / f"{index:02d}" / "design.json"
    driver.write_json(path, design)
    return path


# ---- drawing and review --------------------------------------------------------------------------------------

def fields(d: dict) -> dict:
    lines = [f"- {AXIS_LABELS.get(a, a.capitalize())}: {AXES[a][o]}." for a, o in d["arrangement"].items()]
    if d.get("arrange"):
        lines.append(f"- Inside the task area: {d['arrange']['text']}")
    mode = ("native desktop application with authentic window, menu and control conventions"
            if d["platform"] == "desktop" else "web application page shown without any browser chrome (no address "
            "bar, browser tabs, bookmarks bar or browser menus; a menu bar, if any, is the app's own)")
    return {**{k: v for k, v in d.items() if isinstance(v, str)}, "mode": mode, "w": d["size"][0], "h": d["size"][1],
            "inputs": ", ".join(d["inputs"]), "style_desc": d["style"]["desc"], "regions": "\n".join(lines),
            "start_label": "a documented mid-task state" if d["start"] == "scene" else "the start of the episode",
            "screen": d["screen"] or "the task widget in its unsolved initial state",
            "never": "; ".join(n.strip().rstrip(".") for n in d["never"]) or "nothing beyond the usual",
            "hint": f"Also (where it agrees with the above): {d['hint']}" if d.get("hint") else ""}


def image_problems(path: Path) -> list[str]:
    """Deterministic checks before review: 16:9 within 10% (resized to SIZE in place when it is), not blank."""
    img = Image.open(path).convert("RGB")
    ratio = (img.width / img.height) / (SIZE[0] / SIZE[1])
    if abs(ratio - 1) > 0.10:
        return [f"aspect {img.width}x{img.height} is not 16:9"]
    if max(ImageStat.Stat(img).stddev) < 8:
        return ["the image is blank"]
    if img.size != SIZE:
        img.resize(SIZE, Image.LANCZOS).save(path)
    return []


def draw(design_path: Path, painter: Runtime, reviewer: Runtime, attempts: int = 4) -> dict:
    """Draw, check and review until accepted or out of attempts. Writes attempt-NN.png and their reviews next to
    design.json, design.png when accepted, and the outcome in design.json ("status", "attempts")."""
    d = json.loads(design_path.read_text())
    folder, f = design_path.parent, fields(d)
    prompt = squeeze(template("image").format(**f))
    (folder / "prompt.txt").write_text(prompt)
    # current: the image a revise edits (None: draw fresh); correction: the open review correction. An edit fixes
    # one thing and keeps the rest; a fresh image (after a reject, and always last) carries the correction along.
    history, current, correction = [], None, ""
    for n in range(1, attempts + 1):
        shot = folder / f"attempt-{n:02d}.png"
        fresh = current is None or n == attempts
        if fresh:
            ask = prompt + (f"\nAlso make sure: {correction}\n" if correction else "")
        else:
            ask = squeeze(template("revise").format(note=correction, original=prompt))
        try:
            meta = painter.image(ask, shot, refs=[] if fresh else [current])
        except AgentError as exc:
            history.append({"attempt": n, "error": str(exc)})
            continue
        problems = image_problems(shot)
        if problems:
            history.append({"attempt": n, "image": shot.name, "problems": problems})
            current = None
            continue
        try:
            review = reviewer.ask(squeeze(template("review").format(**f)), REVIEW_SCHEMA, images=[shot])
        except AgentError as exc:
            history.append({"attempt": n, "image": shot.name, "review_error": str(exc)})
            continue
        history.append({"attempt": n, "image": shot.name, "fresh": fresh, "review": review, "seconds": meta["seconds"]})
        if review["verdict"] == "accept":
            (folder / "design.png").write_bytes(shot.read_bytes())
            break
        correction = review["correction"]
        current = shot if review["verdict"] == "revise" and correction else None
    d["status"] = "accepted" if (folder / "design.png").is_file() else "failed"
    d["attempts"] = history
    driver.write_json(design_path, d)
    return d
