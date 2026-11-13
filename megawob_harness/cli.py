"""megawob-harness: build new MegaWoB design-* sites with coding agents, from a design image to a site that passes
the checker.

    megawob-harness new set-slider horizontal              # design image, then the site
    megawob-harness design use-email archive --count 3     # only design images (reviewed)
    megawob-harness build data/sitegen/designs/use-email/archive/07/design.json
    megawob-harness card use-email                         # rebuild a task's capability card
    megawob-harness status                                 # designs and builds so far

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


def design_paths(args) -> list[Path]:
    """Plan and draw --count designs of task/scenario; returns the accepted ones."""
    painter = get_runtime("codex", args.image_model, "low")  # Codex has the image tool
    reviewer = get_runtime(args.review_runtime, args.review_model, args.effort)
    plans = [designs.plan(args.task, args.scenario, args.out, args.style) for _ in range(args.count)]

    def one(path: Path) -> Path | None:
        d = designs.draw(path, painter, reviewer, args.attempts)
        tries = len(d["attempts"])
        print(f"{d['site']}: design {d['status']} after {tries} image{'s' * (tries != 1)}  ({path.parent})", flush=True)
        return path if d["status"] == "accepted" else None

    with ThreadPoolExecutor(args.jobs) as pool:
        return [p for p in pool.map(one, plans) if p]


def build_all(args, paths: list[Path]) -> None:
    builder = get_runtime(args.runtime, args.model, args.effort)
    reviewer = get_runtime(args.review_runtime, args.review_model, args.effort)
    polisher = None if args.no_polish else get_runtime(args.runtime, args.polish_model, args.effort)

    settings = build.Settings(minutes=args.minutes, fix_rounds=args.fix_rounds, seeds=args.seeds)

    def one(path: Path) -> dict:
        return build.build(path, args.out, builder, reviewer, polisher, settings, args.force, args.fix_model)

    with ThreadPoolExecutor(args.jobs) as pool:
        records = list(pool.map(one, paths))
    for r in records:
        polish = f", polish {'kept' if r['polish']['kept'] else 'reverted'}" if r.get("polish") else ""
        print(f"{r['site']}: {'PASSED' if r['passed'] else 'FAILED'} in {r['minutes']} min "
              f"({len(r['turns'])} turn{'s' * (len(r['turns']) != 1)}{polish}), review fidelity {r['fidelity']}; "
              f"record {args.out / 'runs' / (r['site'] + '.json')}")
        if not r["passed"]:
            print(f"  gate: {r['turns'][-1]['gate'][:3]}\n  fix it by hand (python -m megawob.check --site {r['site']})"
                  f", rerun with --force, or remove html/sites/{r['site']} and run python -m megawob.scenarios")


def planned(out: Path):
    """(path, design) of every design planned in `out`."""
    for path in sorted((out / "designs").glob("*/*/*/design.json")):
        yield path, json.loads(path.read_text())


def resolve(item: str, out: Path) -> Path:
    """A design.json path, or a site name planned in out."""
    if item.endswith(".json"):
        return Path(item)
    path = next((path for path, d in planned(out) if d["site"] == item), None)
    if path is None:
        raise SystemExit(f"{item}: no design.json for this site under {out / 'designs'}")
    return path


def status(out: Path) -> None:
    for _, d in planned(out):
        run = out / "runs" / f"{d['site']}.json"
        built = "not built"
        if run.is_file():
            r = json.loads(run.read_text())
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
        p.add_argument("task", choices=tasks, metavar="task")
        p.add_argument("scenario")
        p.add_argument("--count", type=int, default=1)
        p.add_argument("--style", choices=designs.PROFILES, metavar="SLUG",
                       help="a style profile slug from styles.json (default: the least used)")
        p.add_argument("--attempts", type=int, default=4, help="images per design before giving up")
    p = sub.add_parser("build", help="build sites from accepted designs")
    p.add_argument("designs", nargs="+", help="design.json paths or planned site names")
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
        accepted = design_paths(args)
        if args.cmd == "new" and accepted:
            build_all(args, accepted)
    elif args.cmd == "build":
        build_all(args, [resolve(item, args.out) for item in args.designs])
    else:
        status(args.out)


if __name__ == "__main__":
    main()
