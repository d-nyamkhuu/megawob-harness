"""megawob-harness: build new MegaWoB design-* sites with coding agents, from a design image to a site that passes
the checker.

    megawob-harness new set-slider horizontal              # design image, then the site
    megawob-harness design use-email archive --count 3     # only design images (reviewed)
    megawob-harness build data/sitegen/designs/use-email/archive/07/design.json
    megawob-harness card use-email                         # rebuild a task's capability card
    megawob-harness status                                 # designs and builds so far

Batches (one process plans the whole batch, then --jobs designs draw and build at a time):

    megawob-harness --jobs 4 new my-task --count 2         # 2 designs for every scenario of a task
    megawob-harness --jobs 8 new --all                     # 1 more design for every task and scenario
    megawob-harness --jobs 4 build --pending               # build every accepted design that has no site

Stages (each one resumable; see the README):
- card: what the task's code renders per scenario (cards/<task>.json; every current task has one).
- design: plan a design (style, window arrangement, a real goal), draw it with Codex's image tool, check it and
  have a separate turn review it (accept, revise or redraw), into <out>/designs/<task>/<scenario>/<NN>/.
- build: scaffold html/sites/design-<task>-<scenario>-<NN>/ from the base site, let an agent (Codex or Claude)
  restyle it to the image with render and check tools, then gate it (lint and megawob.check at
  every training viewport) and send failures back for up to --fix-rounds rounds. The run record goes to
  <out>/runs/<site>.json.

Models (defaults): the build turn --model (gpt-6-luna), the gate's fix rounds --fix-model (gpt-6.1-terra), the
polish pass --polish-model (gpt-6.1-sol; --no-polish skips it), design and site reviews and capability cards
--review-model (gpt-6.1-sol), the image tool's relay --image-model (gpt-6-luna). With --runtime claude (and Claude
models), Claude builds; images always come from Codex. Sign in once with `codex login` (and `claude login` for
Claude). Run it from the root of your megawob checkout (installed with pip install -e).
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from megawob import catalog

from . import build, designs
from .runtime import get_runtime


def pairs(task: str | None, scenarios: list[str], every: bool) -> list[tuple[str, str]]:
    """The (task, scenario) pairs to design: the given scenarios of `task` (all of them if none are given), or with
    `every` all scenarios of every task that has a capability card. Checked before anything is planned, so a typo
    does not leave half a batch planned."""
    if every == bool(task):
        raise SystemExit("name a task (and optionally scenarios), or pass --all")
    tasks = [t for t in catalog.tasks() if designs.card_path(t).is_file()] if every else [task]
    out = []
    for t in tasks:
        have = designs.scenarios(t)
        if bad := sorted(set(scenarios) - set(have)):
            raise SystemExit(f"{t} has no scenario {', '.join(bad)}; it has {', '.join(have)}")
        out += [(t, s) for s in scenarios or have]
    return out


def planned(out: Path):
    """(path, design) of every design planned in `out`."""
    for path in sorted((out / "designs").glob("*/*/*/design.json")):
        yield path, json.loads(path.read_text())


def pending(out: Path) -> list[Path]:
    """Accepted designs in `out` whose site does not exist yet. An existing site is never pending, even one whose
    build was interrupted (`status` shows it as not built; build --force rebuilds it)."""
    return [path for path, d in planned(out)
            if d.get("status") == "accepted" and not (catalog.HTML / "sites" / d["site"]).exists()]


def resolve(item: str, out: Path) -> Path:
    """A design.json path, or a site name planned in out."""
    if item.endswith(".json"):
        return Path(item)
    path = next((path for path, d in planned(out) if d["site"] == item), None)
    if path is None:
        raise SystemExit(f"{item}: no design.json for this site under {out / 'designs'}")
    return path


def report(r: dict, out: Path) -> None:
    polish = f", polish {'kept' if r['polish']['kept'] else 'reverted'}" if r.get("polish") else ""
    print(f"{r['site']}: {'PASSED' if r['passed'] else 'FAILED'} in {r['minutes']} min "
          f"({len(r['turns'])} turn{'s' * (len(r['turns']) != 1)}{polish}), review fidelity {r['fidelity']}; "
          f"record {out / 'runs' / (r['site'] + '.json')}", flush=True)
    if not r["passed"]:
        print(f"  gate: {r['turns'][-1]['gate'][:3]}\n  fix it by hand (python -m megawob.check --site {r['site']})"
              f", rerun with --force, or remove html/sites/{r['site']} and run python -m megawob.scenarios", flush=True)


def run(args, paths: list[Path], draw: bool, make: bool) -> None:
    """Draw (if `draw`) and build (if `make`) each design, --jobs at a time; a design is built as soon as its image
    is accepted. One design's error is reported and does not stop the others; the exit status is 1 if any had one."""
    painter = get_runtime("codex", args.image_model, "low") if draw else None  # Codex has the image tool
    reviewer = get_runtime(args.review_runtime, args.review_model, args.effort)
    if make:
        builder = get_runtime(args.runtime, args.model, args.effort)
        polisher = None if args.no_polish else get_runtime(args.runtime, args.polish_model, args.effort)
        settings = build.Settings(minutes=args.minutes, fix_rounds=args.fix_rounds, seeds=args.seeds)

    def one(path: Path) -> str:
        try:
            if draw:
                d = designs.draw(path, painter, reviewer, args.attempts)
                tries = len(d["attempts"])
                print(f"{d['site']}: design {d['status']} after {tries} image{'s' * (tries != 1)}  ({path.parent})",
                      flush=True)
                if d["status"] != "accepted":
                    return "rejected"
            if not make:
                return "accepted"
            r = build.build(path, args.out, builder, reviewer, polisher, settings, args.force, args.fix_model)
            report(r, args.out)
            return "passed" if r["passed"] else "failed"
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - one design must not stop a batch
            print(f"{path.parent}: ERROR {type(exc).__name__}: {exc}", flush=True)
            return "error"

    with ThreadPoolExecutor(args.jobs) as pool:
        outcomes = list(pool.map(one, paths))
    counts = {k: outcomes.count(k) for k in ("accepted", "rejected", "passed", "failed", "error")}
    print(", ".join([f"{len(paths)} designs"] + [f"{n} {k}" for k, n in counts.items() if n]))
    if "error" in outcomes:
        raise SystemExit(1)


def status(out: Path) -> None:
    for _, d in planned(out):
        run_path = out / "runs" / f"{d['site']}.json"
        built = "not built"
        if run_path.is_file():
            r = json.loads(run_path.read_text())
            built = f"built, gate {'passed' if r['passed'] else 'FAILED'}, review fidelity {r.get('fidelity')}"
        print(f"{d['site']:<48} design {d.get('status', 'planned'):<9} {built}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="megawob-harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("data/sitegen"), help="designs and run records")
    parser.add_argument("--runtime", default="codex", choices=["codex", "claude"],
                        help="the agent runtime that builds, fixes and polishes")
    parser.add_argument("--review-runtime", default="codex", choices=["codex", "claude"],
                        help="the runtime of design and site reviews and capability cards")
    parser.add_argument("--model", default="gpt-6-luna", help="the build turn")
    parser.add_argument("--fix-model", default="gpt-6.1-terra", help="fix rounds after a failed gate (same thread)")
    parser.add_argument("--polish-model", default="gpt-6.1-sol", help="the polish pass after a passing build")
    parser.add_argument("--no-polish", action="store_true", help="skip the polish pass")
    parser.add_argument("--review-model", default="gpt-6.1-sol",
                        help="design and site reviews, capability cards (best a different model from the builder)")
    parser.add_argument("--image-model", default="gpt-6-luna", help="relays the prompt to the image tool")
    parser.add_argument("--effort", help="reasoning effort for the build agent and the review")
    parser.add_argument("--jobs", type=int, default=1, help="designs or sites in parallel")
    sub = parser.add_subparsers(dest="cmd", required=True)
    tasks = catalog.tasks()
    p = sub.add_parser("card", help="(re)build capability cards")
    p.add_argument("tasks", nargs="+", choices=tasks, metavar="task")
    for name in ("design", "new"):
        p = sub.add_parser(name, help="plan and draw designs" + (", then build their sites" if name == "new" else ""))
        p.add_argument("task", nargs="?", choices=tasks, metavar="task", help="the task (or --all)")
        p.add_argument("scenarios", nargs="*", metavar="scenario", help="its scenarios (default: all of them)")
        p.add_argument("--all", action="store_true", help="every scenario of every task with a capability card")
        p.add_argument("--count", type=int, default=1, help="designs per scenario")
        p.add_argument("--style", choices=designs.PROFILES, metavar="SLUG",
                       help="a style profile slug from styles.json (default: the least used)")
        p.add_argument("--attempts", type=int, default=4, help="images per design before giving up")
    p = sub.add_parser("build", help="build sites from accepted designs")
    p.add_argument("designs", nargs="*", help="design.json paths or planned site names")
    p.add_argument("--pending", action="store_true", help="every accepted design in --out that has no site yet")
    defaults = build.Settings()
    for p in (sub.choices["new"], sub.choices["build"]):
        p.add_argument("--minutes", type=int, default=defaults.minutes, help="the agent's time for the first turn")
        p.add_argument("--fix-rounds", type=int, default=defaults.fix_rounds,
                       help="gate failures sent back at most this often")
        p.add_argument("--seeds", type=int, default=defaults.seeds,
                       help="plain seeds per viewport in the gate's checker run")
        p.add_argument("--force", action="store_true", help="rebuild a site that exists, from scratch")
    sub.add_parser("status", help="list designs and builds")
    args = parser.parse_args()

    if args.cmd == "card":
        agent = get_runtime(args.review_runtime, args.review_model, args.effort)
        for task in args.tasks:
            print(designs.make_card(agent, task))
    elif args.cmd in ("design", "new"):
        # Planned one after another, so each allocation sees the designs planned before it in this batch.
        paths = [designs.plan(t, s, args.out, args.style)
                 for t, s in pairs(args.task, args.scenarios, args.all) for _ in range(args.count)]
        run(args, paths, draw=True, make=args.cmd == "new")
    elif args.cmd == "build":
        if args.pending == bool(args.designs):
            parser.error("build takes design.json paths or site names, or --pending")
        paths = pending(args.out) if args.pending else [resolve(item, args.out) for item in args.designs]
        run(args, paths, draw=False, make=True)
    else:
        status(args.out)


if __name__ == "__main__":
    main()
