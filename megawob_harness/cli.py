"""megawob-harness: new MegaWoB sites from design images. An AI agent builds each site; automated checks decide
when it is done.

A task is one kind of web interaction in MegaWoB (use-email, set-slider, ...); a scenario is one variant of a task
(use-email archive). Each new site starts as a design image for one task and scenario.

    megawob-harness new use-email archive                # draw a new design image, then build its site
    megawob-harness design use-email archive --count 3   # only draw 3 design images (each one reviewed)
    megawob-harness build design-use-email-archive-07    # build the site of a design drawn earlier
    megawob-harness status                               # list every design and its site, then totals
    megawob-harness capabilities my-task                 # once per new task: write down what it can show

Batches: the whole batch is planned first, then --workers designs are drawn and built at the same time.

    megawob-harness new my-task --count 2 --workers 4    # 2 new designs for every scenario of a task
    megawob-harness new --all --dry-run                  # show the plan for 1 more design per scenario
    megawob-harness new --all --workers 8                # 1 more design for every task and scenario
    megawob-harness new --resume --workers 8             # continue a stopped batch, retrying failed builds

Ctrl-C starts no new design and waits for the running ones to stop; a second Ctrl-C quits at once. Each batch's
output also goes to <out>/logs/.

What each step does (see the README):
- capabilities: a model reads the task's code and writes down what it shows at the start of each scenario and
  what it never shows, so design images ask for nothing more (<out>/cards/<task>.json, used instead of the card
  installed with the package; it has one for every current task).
- design: plan a design (style, window arrangement, a real goal), draw it with Codex's image tool and have a
  separate model review it (accept, edit or redraw), into <out>/designs/<task>/<scenario>/<NN>/.
- build: copy MegaWoB's base site to html/sites/design-<task>-<scenario>-<NN>/ (MegaWoB's checks run there), let
  an agent (Codex or Claude) restyle it to the image, then run the checks and send what fails back for up to
  --fix-rounds more turns. The finished site is copied to <out>/sites/<site>/, the run record to
  <out>/runs/<site>.json.

Models (defaults): the build turn --model (gpt-6-luna), the fix turns --fix-model (gpt-6.1-terra), the polish pass
--polish-model (gpt-6.1-sol; --no-polish skips it), reviews and capabilities --review-model (gpt-6.1-sol), the image
tool's relay --image-model (gpt-6-luna). With --runtime claude (and Claude models), Claude builds; images always
come from Codex. Sign in once with `codex login` (and `claude login` for Claude). <out> defaults to ./sitegen.
"""
import argparse
import json
import logging
import os
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from megawob import catalog

from . import build, designs, gate
from .runtime import get_runtime

log = logging.getLogger("megawob_harness")


def pairs(task: str | None, scenarios: list[str], every: bool, out: Path) -> list[tuple[str, str]]:
    """The (task, scenario) pairs to design: the given scenarios of `task` (all of them if none are given), or with
    `every` all scenarios of every task that has a capability card. Checked before anything is planned, so a typo
    does not leave half a batch planned."""
    if every == bool(task):
        raise SystemExit("name a task (and optionally scenarios), or pass --all")
    tasks = [t for t in catalog.tasks() if designs.card_path(t, out).is_file()] if every else [task]
    selection = []
    for t in tasks:
        have = designs.scenarios(t)
        if bad := sorted(set(scenarios) - set(have)):
            raise SystemExit(f"{t} has no scenario {', '.join(bad)}; it has {', '.join(have)}")
        selection += [(t, s) for s in scenarios or have]
    return selection


def planned(out: Path):
    """(path, design) of every design planned in `out`."""
    for path in sorted((out / "designs").glob("*/*/*/design.json")):
        yield path, json.loads(path.read_text())


def undrawn(out: Path) -> list[Path]:
    """Designs planned in `out` but never drawn (their batch stopped first)."""
    return [path for path, d in planned(out) if "status" not in d]


def unbuilt(out: Path) -> list[Path]:
    """Accepted designs in `out` whose site exists neither in <out>/sites nor in MegaWoB, or whose last build
    stopped on an error. Any other existing site is never resumed, even one with no run record (`status` shows it
    as not built; build --force rebuilds it): sites built elsewhere have none."""
    def exists(site: str) -> bool:
        return build.saved(out, site).exists() or (catalog.HTML / "sites" / site).exists()
    return [path for path, d in planned(out) if d.get("status") == "accepted" and
            (not exists(d["site"]) or build.errored(out, d["site"]))]


def resolve(item: str, out: Path) -> Path:
    """A design.json path, or a site name planned in out."""
    if item.endswith(".json"):
        return Path(item)
    path = next((path for path, d in planned(out) if d["site"] == item), None)
    if path is None:
        raise SystemExit(f"{item}: no design.json for this site under {out / 'designs'}")
    return path


def dry_run(args, selection: list[tuple[str, str]]) -> None:
    """Plan the batch in a scratch copy of <out>'s designs and cards and print it; nothing is written to <out>."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for name in ("designs", "cards"):
            if (args.out / name).is_dir():
                shutil.copytree(args.out / name, scratch / name, ignore=shutil.ignore_patterns("*.png"))
        for t, s in selection:
            for _ in range(args.count):
                d = json.loads(designs.plan(t, s, scratch, args.style).read_text())
                print(f"{d['site']:<46} {d['id'].split('/', 2)[2]}")
    print(f"{len(selection) * args.count} designs would be planned ({len(selection)} scenarios); nothing written")


def dry_run_resume(paths: list[Path], make: bool) -> None:
    """Print what --resume would do; nothing runs."""
    for path in paths:
        d = json.loads(path.read_text())
        step = "build" if d.get("status") == "accepted" else "draw, then build" if make else "draw"
        print(f"{d['site']:<46} {step}")
    print(f"{len(paths)} designs would be resumed; nothing run")


# Memory one worker may need at its peak: the agent (~0.2 GB, growing over a long session), its tools server, and a
# render (~0.3 GB) or a checker run (~0.4 GB). Measured in the README's sizing note; Anthropic sizes 1 GiB an agent.
GB_PER_WORKER = 1.0


def available_gb() -> float | None:
    """The memory free for new work, from /proc/meminfo (None where there is none)."""
    try:
        with open("/proc/meminfo") as f:
            return next(int(line.split()[1]) for line in f if line.startswith("MemAvailable:")) / 2**20
    except (OSError, StopIteration):
        return None


def start_log(out: Path, cmd: str) -> Path:
    """Batch output to the console and to <out>/logs/<time>-<cmd>.log."""
    path = out / "logs" / f"{datetime.now():%Y%m%d-%H%M%S}-{cmd}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    file = logging.FileHandler(path)
    file.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.handlers, log.propagate = [logging.StreamHandler(sys.stdout), file], False
    log.setLevel(logging.INFO)
    return path


def report(r: dict, out: Path) -> None:
    polish = f", polish {'kept' if r['polish']['kept'] else 'reverted'}" if r.get("polish") else ""
    log.info(f"{r['site']}: {'PASSED' if r['passed'] else 'FAILED'} in {r['minutes']} min "
             f"({len(r['turns'])} turn{'s' * (len(r['turns']) != 1)}{polish}), review fidelity {r['fidelity']}; "
             f"record {out / 'runs' / (r['site'] + '.json')}")
    if not r["passed"]:
        log.info(f"  gate: {r['turns'][-1]['gate'][:3]}\n  fix it by hand (python -m megawob.check --site "
                 f"{r['site']}), rerun with --force, or remove html/sites/{r['site']} and run python -m "
                 "megawob.scenarios")


def run(args, paths: list[Path], draw: bool, make: bool) -> None:
    """Draw (if `draw`, unless already accepted) and build (if `make`) each design, --workers at a time; a design is
    built as soon as its image is accepted. One design's error is reported and does not stop the others; the exit
    status is 1 if any had one."""
    logfile = start_log(args.out, args.cmd)
    painter = get_runtime("codex", args.image_model, "low") if draw else None  # Codex has the image tool
    reviewer = get_runtime(args.review_runtime, args.review_model, args.effort)
    if make:
        builder = get_runtime(args.runtime, args.model, args.effort)
        polisher = None if args.no_polish else get_runtime(args.runtime, args.polish_model, args.effort)
        settings = build.Settings(minutes=args.minutes, fix_rounds=args.fix_rounds, seeds=args.seeds)

    def one(path: Path) -> str:
        try:
            if draw and json.loads(path.read_text()).get("status") != "accepted":
                d = designs.draw(path, painter, reviewer, args.attempts)
                tries = len(d["attempts"])
                log.info(f"{d['site']}: design {d['status']} after {tries} image{'s' * (tries != 1)}  ({path.parent})")
                if d["status"] != "accepted":
                    return "rejected"
            if not make:
                return "accepted"
            r = build.build(path, args.out, builder, reviewer, polisher, settings, args.force, args.fix_model)
            report(r, args.out)
            return "passed" if r["passed"] else "failed"
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - one design must not stop a batch
            log.info(f"{path.parent}: ERROR {type(exc).__name__}: {exc}")
            return "error"

    log.info(f"{len(paths)} designs, {args.workers} at a time; log {logfile}")
    free = available_gb()
    if args.workers > 1 and free and args.workers * GB_PER_WORKER > free:
        log.info(f"warning: {args.workers} workers may need about {args.workers * GB_PER_WORKER:.0f} GB and "
                 f"{free:.1f} GB is free; lower --workers (about {max(1, int(free // GB_PER_WORKER))}) if builds fail "
                 "or the machine swaps")
    pool = ThreadPoolExecutor(args.workers)
    try:  # Ctrl-C can land while designs are still being queued
        outcomes = [f.result() for f in [pool.submit(one, path) for path in paths]]
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        gate.stop()  # the agents got the Ctrl-C; the checkers run in sessions of their own
        log.info("interrupted: no new design starts; waiting for the running ones to stop (Ctrl-C again quits now)")
        try:
            pool.shutdown()
        except KeyboardInterrupt:
            os._exit(130)
        log.info("resume with: megawob-harness new --resume (or design --resume, build --resume)")
        raise SystemExit(130) from None
    pool.shutdown()
    counts = Counter(outcomes)
    log.info(", ".join([f"{len(paths)} designs", *(f"{n} {k}" for k, n in counts.most_common())]))
    if counts["error"] or counts["failed"]:
        log.info("rerun: megawob-harness new --resume (undrawn and unbuilt designs), or "
                 "megawob-harness build <site> --force (a failed site)")
    if counts["error"]:
        raise SystemExit(1)


def status(out: Path) -> None:
    """Every design and its build, then a summary."""
    rows = list(planned(out))
    if not rows:
        print(f"no designs under {out / 'designs'}")
        return
    records = []
    for _, d in rows:
        run_path = out / "runs" / f"{d['site']}.json"
        built = "not built"
        if run_path.is_file():
            records.append(r := json.loads(run_path.read_text()))
            built = ("build stopped on an error" if "error" in r else
                     f"built, gate {'passed' if r['passed'] else 'FAILED'}, review fidelity {r.get('fidelity')}")
        print(f"{d['site']:<48} design {d.get('status', 'planned'):<9} {built}")
    states = Counter(d.get("status", "planned") for _, d in rows)
    print(f"\n{len(rows)} designs: " + ", ".join(f"{n} {k}" for k, n in states.most_common()))
    errors = [r for r in records if "error" in r]
    records = [r for r in records if "error" not in r]
    if errors:
        print(f"{len(errors)} builds stopped on an error (new --resume or build --resume retries them)")
    if records:
        passed = [r for r in records if r["passed"]]
        direct = sum(not any(t["stage"].endswith("fix") for t in r["turns"]) for r in passed)
        scores = [r["fidelity"] for r in passed if r.get("fidelity")]
        print(f"{len(records)} built: {len(passed)} passed the gate ({len(passed) / len(records):.0%}), {direct} of "
              f"them without a fix round; median {statistics.median(r['minutes'] for r in records)} min a site"
              + (f"; mean review fidelity {statistics.mean(scores):.1f}" if scores else ""))


def shared_options(p: argparse.ArgumentParser, defaults: bool = True) -> None:
    """Options every command takes, before or after its name. A subcommand's copies default to SUPPRESS, so they
    only override what was given before the name when given themselves."""
    def default(value):
        return value if defaults else argparse.SUPPRESS
    p.add_argument("--out", type=Path, default=default(Path("sitegen")),
                   help="designs, finished sites, run records and logs (default: ./sitegen)")
    p.add_argument("--runtime", default=default("codex"), choices=["codex", "claude"],
                   help="the agent runtime that builds, fixes and polishes")
    p.add_argument("--review-runtime", default=default("codex"), choices=["codex", "claude"],
                   help="the runtime of design and site reviews and capability cards")
    p.add_argument("--model", default=default("gpt-6-luna"), help="the build turn")
    p.add_argument("--fix-model", default=default("gpt-6.1-terra"), help="fix rounds after a failed gate (same thread)")
    p.add_argument("--polish-model", default=default("gpt-6.1-sol"), help="the polish pass after a passing build")
    p.add_argument("--no-polish", action="store_true", default=default(False), help="skip the polish pass")
    p.add_argument("--review-model", default=default("gpt-6.1-sol"),
                   help="design and site reviews, capability cards (best a different model from the builder)")
    p.add_argument("--image-model", default=default("gpt-6-luna"), help="relays the prompt to the image tool")
    p.add_argument("--effort", default=default(None), help="reasoning effort for the build agent and the review")
    p.add_argument("--workers", type=int, default=default(1), help="designs or sites built in parallel")


def main() -> None:
    parser = argparse.ArgumentParser(prog="megawob-harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    shared_options(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    tasks = catalog.tasks()
    p = sub.add_parser("capabilities", help="write down what a task can show in each scenario (once per new task)")
    p.add_argument("tasks", nargs="+", choices=tasks, metavar="task")
    for name in ("design", "new"):
        p = sub.add_parser(name, help="draw and review design images" + (", then build their sites" if name == "new"
                                                                         else " (no sites)"))
        p.add_argument("task", nargs="?", choices=tasks, metavar="task",
                       help="the task, e.g. use-email (or --all, or --resume)")
        p.add_argument("scenarios", nargs="*", metavar="scenario",
                       help="its scenarios, e.g. archive (default: all of them)")
        p.add_argument("--all", action="store_true", help="every scenario of every task")
        built = ", and build designs with no site yet or whose build stopped on an error" if name == "new" else ""
        p.add_argument("--resume", action="store_true",
                       help=f"continue a stopped batch: draw the designs it planned but never drew{built}")
        p.add_argument("--dry-run", action="store_true", help="show what would be done; change nothing")
        p.add_argument("--count", type=int, default=1, help="designs per scenario (default 1)")
        p.add_argument("--style", choices=designs.PROFILES, metavar="SLUG",
                       help="a style profile slug from styles.json (default: the least used)")
        p.add_argument("--attempts", type=int, default=4, help="images drawn per design before it is rejected")
    p = sub.add_parser("build", help="build the sites of designs drawn earlier")
    p.add_argument("designs", nargs="*", help="site names (e.g. design-use-email-archive-07) or design.json paths")
    p.add_argument("--resume", action="store_true",
                   help="every accepted design with no site yet, or whose build stopped on an error")
    defaults = build.Settings()
    for p in (sub.choices["new"], sub.choices["build"]):
        p.add_argument("--minutes", type=int, default=defaults.minutes,
                       help="time limit of the agent's first turn (fix turns get half)")
        p.add_argument("--fix-rounds", type=int, default=defaults.fix_rounds,
                       help="how many times failed checks go back to the agent")
        p.add_argument("--seeds", type=int, default=defaults.seeds, help="seeds per screen size in the final check")
        p.add_argument("--force", action="store_true", help="rebuild a site that already exists, from scratch")
    sub.add_parser("status", help="list every design and its site, then totals")
    for p in sub.choices.values():
        shared_options(p, defaults=False)
    args = parser.parse_args()
    args.out = args.out.resolve()  # paths under it go to the agents' tools, which run in other folders

    if args.cmd == "capabilities":
        agent = get_runtime(args.review_runtime, args.review_model, args.effort)
        for task in args.tasks:
            print(designs.make_card(agent, task, args.out))
    elif args.cmd in ("design", "new"):
        if args.resume:
            if args.task or args.all:
                parser.error("--resume takes no task and no --all")
            paths = undrawn(args.out) + (unbuilt(args.out) if args.cmd == "new" else [])
            if args.dry_run:
                return dry_run_resume(paths, make=args.cmd == "new")
        else:
            selection = pairs(args.task, args.scenarios, args.all, args.out)
            if args.dry_run:
                return dry_run(args, selection)
            # Planned one after another, so each allocation sees the designs planned before it in this batch.
            paths = [designs.plan(t, s, args.out, args.style) for t, s in selection for _ in range(args.count)]
        run(args, paths, draw=True, make=args.cmd == "new")
    elif args.cmd == "build":
        if args.resume == bool(args.designs):
            parser.error("build takes site names or design.json paths, or --resume")
        paths = unbuilt(args.out) if args.resume else [resolve(item, args.out) for item in args.designs]
        run(args, paths, draw=False, make=True)
    else:
        status(args.out)


if __name__ == "__main__":
    main()
