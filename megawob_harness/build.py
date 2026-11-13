"""Build a design-* site from an accepted design image: scaffold, build, gate and fix, review, polish.

No agent's own verdict accepts anything:

1. **Scaffold** `html/sites/design-<task>-<scenario>-<NN>/` from the base site: site.js a design on
   templates/site/base.js, the widget a design on the task's template for the scenario (both `design: {}`), so the
   page boots and passes the checker before the agent changes anything. The site is added to html/scenarios.json.
2. **Build.** An agent (Codex or Claude) restyles it to the design image, in the site folder only, with the
   `render` and `check` tools (tools.py, outside its sandbox), and ends with a JSON report.
3. **Gate and fix.** gate.full (files, syntax, lint, megawob.check at every training viewport) decides, whatever the
   agent reported. Its failures go back to the same thread as fix rounds on the fix model, up to `fix_rounds` times.
   Because the scaffold passes the gate untouched, a first turn that reports `done: false` (it ran out of time, say)
   goes back too.
4. **Review.** A separate read-only turn on the reviewer (which did not build the page) scores its fidelity 1-5
   from renders and lists what still differs.
5. **Polish.** A stronger model in a fresh thread works from that list, then the gate (one fix round) and the
   review run again. The polish is kept only if the gate passes and the fidelity did not drop; otherwise the built
   files are restored. Skipped when the review gives 5.

The run record (every turn's model, status, time, tokens, cost, tool calls, report and gate result, both reviews,
whether the polish was kept) goes to <out>/runs/<site>.json; a KIT_REQUESTS.md the
agent writes is moved next to it, so the site folder holds only site files.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from megawob import catalog
from PIL import Image

from . import gate
from .jsonio import PROMPTS, fill, parse_json, prompt, write_json
from .render import Renderer
from .runtime.base import AgentError, Runtime, SessionSpec

SITES, TEMPLATES = gate.SITES, catalog.HTML / "templates"
REPORT_SCHEMA = json.loads((PROMPTS / "result.schema.json").read_text())
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["fidelity", "differences", "frame_issues"],
    "properties": {"fidelity": {"type": "integer", "minimum": 1, "maximum": 5},
                   "differences": {"type": "array", "items": {"type": "string"}},
                   "frame_issues": {"type": "array", "items": {"type": "string"}}},
}
NOT_DONE = "your report says done=false (or is missing): finish the remaining differences, then report again"
FIX = """The final gate failed. These checks must pass (the lint, then megawob.check at every training viewport with
more seeds than `check()` runs):
{fails}

Reproduce a failure with `render(seed=N, params={...}, play=K)` before editing, fix what it shows, then call
`check()` until it passes. Keep the look you built. Reply with the same JSON report as before."""
POLISH = """This site was built from the design image by another agent and passes every check. Your job is to raise its
fidelity. An independent review of the current page gave it {fidelity} of 5 and lists what still differs from the
design image:
{differences}
Problems in the episode settings (instruction modes, end control, decoys):
{frame}
Close as many of these as you can, the task widget as much as the frame, without breaking what works: the checks
must still pass (call `check()` before you finish). The brief the site was built from follows."""


@dataclass
class Settings:
    minutes: int = 60
    fix_rounds: int = 2
    seeds: int = 5


@dataclass(frozen=True)
class Job:
    """An accepted design (its design.json, from designs.plan and designs.draw) and the site it becomes."""
    path: Path
    design: dict
    site: str
    task: str
    scenario: str
    image: Path

    @classmethod
    def load(cls, path: Path) -> Job:
        path = Path(path)
        d = json.loads(path.read_text())
        if d.get("status") != "accepted":
            raise SystemExit(f"{path}: the design is not accepted (run `megawob-harness design` first)")
        return cls(path, d, d["site"], d["task"], d["scenario"], path.parent / "design.png")

    @property
    def folder(self) -> Path:
        return SITES / self.site


def bullets(items, empty: str = "") -> str:
    return "\n".join(f"- {x}" for x in items) or empty


# ---- scaffold ------------------------------------------------------------------------------------------------

def task_template(task: str, scenario: str) -> str:
    """The template a new widget for `scenario` is a design on: templates/<task>/base.js when the task has one,
    else the first of its templates that names the scenario (`design: {}` then draws that template's pilot)."""
    if (TEMPLATES / task / "base.js").is_file():
        return "base"
    for path in sorted((TEMPLATES / task).glob("[!_]*.js")):  # _*.js are shared libraries
        if re.search(rf"(?<![\w-]){re.escape(scenario)}(?![\w-])", path.read_text()):
            return path.stem
    raise SystemExit(f"{task}: no widget template names scenario {scenario!r} (html/templates/{task}/)")


def scaffold(site: str, task: str, scenario: str, description: str) -> None:
    """A new site from the base site's frame and its widget for the scenario, as designs on their templates."""
    dest, base = SITES / site, SITES / "base"
    (dest / "widgets").mkdir(parents=True)
    (dest / "site.js").write_text(
        "// Design on templates/site/base.js: the base site's frame and overlays, unchanged. Set its tokens and\n"
        "// replace the parts the design draws differently (design: { TITLES, parts: { name: (base, S) => ... } };\n"
        "// the header of the template lists them).\n"
        f"env.site({{ name: {site!r}, template: 'site/base', design: {{}} }});\n")
    shutil.copy2(base / "site.css", dest / "site.css")
    tpl = task_template(task, scenario)
    (dest / "widgets" / f"{task}.js").write_text(
        f"// Design on templates/{task}/{tpl}.js, unchanged. Restyle it in site.css, set the template's tokens and\n"
        "// replace the parts the design draws differently (design: { parts: { name: (base, S) => replacement } };\n"
        "// html/templates/README.md).\n"
        f"env.widget({{\n  task: {task!r},\n  site: {site!r},\n  name: 'design',\n  split: 'train',\n"
        f"  scenarios: [{scenario!r}],\n  template: '{task}/{tpl}',\n  design: {{}},\n}});\n")
    _embed_widgets(task, base, dest)
    write_json(dest / "manifest.json", {"site": site, "split": "train", "description": description,
                                        "widgets": {task: [{"name": "design", "split": "train"}]}})
    gate.index(site)


def _embed_widgets(task: str, base: Path, dest: Path) -> None:
    """A composite widget loads the widgets it embeds (`uses: [[task, widget], ...]`) from the site's own widgets/:
    designs by name on their task's base template where there is one, else copies of the base site's."""
    widget = base / "widgets" / f"{task}.js"
    code = widget.read_text() if widget.is_file() else ""
    base_widgets = json.loads((base / "manifest.json").read_text())["widgets"]
    for sub in sorted(set(re.findall(r"\[\s*'([a-z][a-z-]*)'\s*,\s*'[\w-]+'\s*\]", code)) - {task}):
        source = base / "widgets" / f"{sub}.js"
        if not source.is_file() or not (catalog.HTML / "tasks" / sub).is_dir():
            continue
        names = [w["name"] for w in base_widgets.get(sub, [])]
        if (TEMPLATES / sub / "base.js").is_file() and names:
            (dest / "widgets" / f"{sub}.js").write_text(
                f"// Designs on templates/{sub}/base.js: the base site's {sub} widgets, unchanged; this composite\n"
                "// site embeds them by name.\n"
                f"for (const name of {names!r}) {{\n"
                f"  env.widget({{ task: {sub!r}, name, template: '{sub}/base', design: {{ widget: name }} }});\n}}\n")
        else:
            shutil.copy2(source, dest / "widgets" / f"{sub}.js")


# ---- the build -----------------------------------------------------------------------------------------------

def brief(job: Job, minutes: int) -> str:
    """The build agent's first message (prompts/build.md)."""
    d = job.design
    width, height = Image.open(job.image).size
    intent = "\n".join([d["id"], f"style: {d['era']} {d['style']['desc']}",
                        *(f"{axis}: {option}" for axis, option in d["arrangement"].items()),
                        *([f"task area: {d['arrange']['text']}"] if d.get("arrange") else []),
                        f"shows: {d['screen']}", f"sample goal: {d['sample']}"])
    files = "\n".join(f"{p.relative_to(job.folder)}  ({p.stat().st_size} bytes)"
                      for p in sorted(job.folder.rglob("*")) if p.is_file())
    return prompt("build.md", site=job.site, task=job.task, scenario=job.scenario, minutes=minutes, width=width,
                  height=height, intent=intent, file_map=files, schema=json.dumps(REPORT_SCHEMA, indent=1))


def polish_note(review: dict | None) -> str:
    review = review or {}
    return fill(POLISH, fidelity=review.get("fidelity", "?"),
                differences=bullets(review.get("differences", []), "- (none listed)"),
                frame=bullets(review.get("frame_issues", []), "- (none)"))


class SiteBuild:
    """The agent threads on one site, the reviews of it, and the turns of its run record."""

    def __init__(self, job: Job, settings: Settings, fix_model: str | None = None):
        self.job, self.settings, self.fix_model = job, settings, fix_model
        self.turns: list[dict] = []

    def thread(self, runtime: Runtime, text: str, stage: str, fix_rounds: int) -> bool:
        """One agent thread on the site: `text` with the design image, then each gate failure back as a fix round
        on the fix model (the thread keeps its context), at most `fix_rounds` times. True once the gate passes."""
        job, s = self.job, self.settings
        workspace = job.folder.resolve()
        with tempfile.TemporaryDirectory(prefix="megawob-harness-") as tmp:
            tools = Path(tmp) / "tools.json"
            tools.write_text(json.dumps({"site": job.site, "task": job.task, "scenario": job.scenario,
                                         "image": str(job.image)}))
            mcp = {"megawob": [sys.executable, "-m", "megawob_harness.tools", str(tools)]}
            with runtime.session(SessionSpec(workspace, mcp)) as session:
                images, seconds, model = [job.image], s.minutes * 60, runtime.model
                for rnd in range(fix_rounds + 1):
                    turn = session.turn(text, images, seconds, model)
                    report, fails = self._judge(turn.text, first=rnd == 0)
                    self.turns.append({
                        "stage": f"{stage} fix" if rnd else stage, "model": model, "status": turn.status,
                        "seconds": turn.seconds, "tool_calls": turn.tool_calls, "usage": turn.usage,
                        "cost_usd": turn.cost_usd, "report": report, "gate": fails})
                    print(f"{job.site}: {stage} turn {rnd + 1} ({model or 'default model'}) {turn.status} in "
                          f"{turn.seconds:.0f} s, gate {f'{len(fails)} failures' if fails else 'passed'}", flush=True)
                    if not fails:
                        return True
                    text, images = fill(FIX, fails=bullets(fails)), []
                    seconds, model = s.minutes * 30, self.fix_model or runtime.model
        return False

    def _judge(self, text: str, first: bool) -> tuple[dict, list[str]]:
        """The turn's report and the failures: the gate's, plus a first turn that is not done (a fix round only has to
        make the gate pass)."""
        try:
            report = parse_json(text, REPORT_SCHEMA)
        except ValueError as exc:
            report = {"error": str(exc)}
        fails = gate.full(self.job.site, self.job.task, self.settings.seeds)
        if first and not report.get("done"):
            fails.append(NOT_DONE + (f"; {report['error']}" if "error" in report else ""))
        return report, fails

    def review(self, reviewer: Runtime | None) -> dict | None:
        """An independent look at the built page (prompts/site_review.txt): fidelity 1-5 and what still differs,
        from the design image, the page beside it and the four instruction modes. None without an answer."""
        if reviewer is None:
            return None
        job = self.job
        with tempfile.TemporaryDirectory(prefix="megawob-harness-review-") as tmp:
            shots = [Path(tmp) / "design.png", Path(tmp) / "modes.png"]
            with Renderer(job.site, job.task, job.scenario, job.image) as r:
                r.compare(0)[0].save(shots[0])
                r.compare(3, "modes")[0].save(shots[1])
            try:
                return reviewer.ask(prompt("site_review.txt"), REVIEW_SCHEMA, images=[job.image, *shots])
            except AgentError as exc:
                print(f"{job.site}: review failed: {exc}", flush=True)
                return None

    def polish(self, polisher: Runtime, reviewer: Runtime | None, before: dict | None, text: str) -> dict:
        """A fresh thread on `polisher` works from the review `before`. Kept only if the gate passes and the
        fidelity did not drop; otherwise the site's files are restored."""
        site = self.job.folder
        with tempfile.TemporaryDirectory(prefix="megawob-harness-keep-") as tmp:
            kept = shutil.copytree(site, Path(tmp) / "site")
            ok = self.thread(polisher, polish_note(before) + "\n\n" + text, "polish", 1)
            after = self.review(reviewer) if ok else None
            keep = ok and (before is None or after is None or after["fidelity"] >= before["fidelity"])
            if not keep:
                shutil.rmtree(site)
                shutil.copytree(kept, site)
        print(f"{self.job.site}: polish {'kept' if keep else 'reverted'} (fidelity "
              f"{before and before['fidelity']} -> {after and after['fidelity']})", flush=True)
        return {"kept": keep, "gate_passed": ok, "review": after}


def build(design_path: Path, out: Path, builder: Runtime, reviewer: Runtime | None = None,
          polisher: Runtime | None = None, settings: Settings | None = None, force: bool = False,
          fix_model: str | None = None) -> dict:
    """Scaffold, build, review and polish the site of one accepted design. Returns the run record, which is also
    written to <out>/runs/<site>.json."""
    job, s = Job.load(design_path), settings or Settings()
    if job.folder.exists():
        if not force:
            raise SystemExit(f"{job.site} exists; pass --force to rebuild it from scratch")
        shutil.rmtree(job.folder)
    t0 = time.monotonic()
    scaffold(job.site, job.task, job.scenario, job.design["id"])
    run = SiteBuild(job, s, fix_model)
    record = {"site": job.site, "design": str(job.path), "started": datetime.now().isoformat(timespec="seconds"),
              "builder": repr(builder), "fixer": fix_model or builder.model, "reviewer": reviewer and repr(reviewer),
              "polisher": polisher and repr(polisher), "settings": asdict(s), "turns": run.turns}
    text = brief(job, s.minutes)
    passed = run.thread(builder, text, "build", s.fix_rounds)
    record["review"] = review = run.review(reviewer) if passed else None
    if passed and polisher and (review is None or review["fidelity"] < 5):
        record["polish"] = polish = run.polish(polisher, reviewer, review, text)
        if polish["kept"] and polish["review"]:
            record["review"] = polish["review"]
    runs = Path(out) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for name in gate.IGNORE:  # the agent's notes go next to the run record
        if (job.folder / name).is_file():
            shutil.move(job.folder / name, runs / f"{job.site}.{name}")
    gate.index(job.site)
    record.update(passed=passed, fidelity=(record["review"] or {}).get("fidelity"),
                  minutes=round((time.monotonic() - t0) / 60, 1))
    write_json(runs / f"{job.site}.json", record)
    return record
