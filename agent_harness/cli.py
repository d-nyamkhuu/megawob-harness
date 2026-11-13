"""agent-harness run | status.

    agent-harness run --domain examples.static_page:StaticPage --job examples/static_page/jobs/pricing.json \\
        --runtime claude --review-runtime codex
    agent-harness status
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .domain import load_domain
from .pipeline import Pipeline, Settings
from .runtime import get_runtime


def _run(args) -> None:
    domain = load_domain(args.domain, json.loads(args.config) if args.config else {})
    builder = get_runtime(args.runtime, args.model, args.effort)
    reviewer = get_runtime(args.review_runtime or args.runtime, args.review_model, args.effort)
    polisher = None if args.no_polish else get_runtime(args.polish_runtime or args.runtime, args.polish_model,
                                                       args.effort)
    settings = Settings(minutes=args.minutes, fix_rounds=args.fix_rounds, polish=not args.no_polish,
                        stop_gate=not args.no_stop_gate, require_proof=not args.no_proof)
    pipeline = Pipeline(domain, builder, reviewer=reviewer, polisher=polisher, fix_model=args.fix_model,
                        settings=settings, out=Path(args.out))
    for path in args.job:
        record = pipeline.run(json.loads(Path(path).read_text()), force=args.force)
        print(f"{record['job']}: {'passed' if record['passed'] else 'FAILED'}, fidelity {record['fidelity']}, "
              f"{record['minutes']} min, {len(record['turns'])} turns")


def _status(args) -> None:
    runs = sorted((Path(args.out) / "runs").glob("*.json"))
    if not runs:
        print(f"no runs in {args.out}/runs")
    for path in runs:
        r = json.loads(path.read_text())
        turns = r["turns"]
        cost = sum(t.get("cost_usd") or 0 for t in turns)
        print(f"{r['job']:<28} {'passed' if r['passed'] else 'FAILED':<7} fidelity {r.get('fidelity') or '-'}  "
              f"turns {len(turns)}  checks {sum(t['tool_calls'].get('check', 0) for t in turns)}  "
              f"stop blocks {sum(t.get('stop_blocks', 0) for t in turns)}  "
              f"polish {'kept' if (r.get('polish') or {}).get('kept') else '-'}  {r['minutes']} min"
              + (f"  ${cost:.2f}" if cost else ""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run jobs through build, gate and fix, review and polish")
    run.add_argument("--domain", required=True, help="module:Class of the domain")
    run.add_argument("--config", help="the domain's config, as JSON")
    run.add_argument("--job", nargs="+", required=True, help="job JSON files")
    run.add_argument("--runtime", default="codex", choices=["codex", "claude"])
    run.add_argument("--model")
    run.add_argument("--effort")
    run.add_argument("--fix-model", help="model for fix rounds (same thread, same runtime)")
    run.add_argument("--review-runtime", choices=["codex", "claude"])
    run.add_argument("--review-model")
    run.add_argument("--polish-runtime", choices=["codex", "claude"])
    run.add_argument("--polish-model")
    run.add_argument("--no-polish", action="store_true")
    run.add_argument("--no-stop-gate", action="store_true", help="let the agent finish without a passing check")
    run.add_argument("--no-proof", action="store_true", help="accept a report without a current check token")
    run.add_argument("--minutes", type=int, default=30)
    run.add_argument("--fix-rounds", type=int, default=2)
    run.add_argument("--out", default="runs")
    run.add_argument("--force", action="store_true", help="redo a job whose workspace exists")
    status = sub.add_parser("status", help="summarize the run records")
    status.add_argument("--out", default="runs")
    args = parser.parse_args(argv)
    {"run": _run, "status": _status}[args.command](args)


if __name__ == "__main__":
    main()
