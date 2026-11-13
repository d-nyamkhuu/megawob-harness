"""Deterministic checks on a design-* site: files, syntax, the static lint and the oracle checker. They decide
whether a build is done; the agent's own report never does.

The quick check (the build agent's `check` tool) is the lint plus megawob.check at 1 seed and its parameter sweep,
at the smallest training viewport first and then a middle and the largest one. The gate checks every training
viewport with more seeds.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

from megawob import catalog

ROOT = catalog.HTML.parent
SITES = catalog.HTML / "sites"
TRAIN_VIEWPORTS = ["1024x640", "1280x720", "1366x768", "1440x900", "1536x864"]
QUICK_VIEWPORTS = ["1024x640", "1366x768", "1536x864"]
#: files the agent may leave in the site folder that are not part of the site (not hashed, moved out after the build)
IGNORE = ("KIT_REQUESTS.md",)


def _megawob(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """python -m megawob.<args> from the repo root, killed with its browsers on timeout."""
    proc = subprocess.Popen([sys.executable, "-m", *args], cwd=ROOT, text=True, start_new_session=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return subprocess.CompletedProcess(proc.args, -9, "", f"timed out after {timeout} s")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def files(site: str, task: str) -> list[str]:
    """Missing files, JavaScript syntax errors and a manifest that does not register the design widget."""
    d, fails = SITES / site, []
    missing = [n for n in ("site.js", "site.css", "manifest.json", f"widgets/{task}.js") if not (d / n).is_file()]
    if missing:
        fails.append(f"missing files: {', '.join(missing)}")
    for js in sorted(d.rglob("*.js")):
        out = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        if out.returncode:
            fails.append(f"node --check {js.relative_to(d)}: {out.stderr.strip().splitlines()[-1:]}")
    try:
        manifest = json.loads((d / "manifest.json").read_text())
        if manifest.get("site") != site or not any(w.get("name") == "design"
                                                   for w in manifest.get("widgets", {}).get(task, [])):
            fails.append(f"manifest.json does not register the design widget of {task} for {site}")
    except (OSError, ValueError) as exc:
        fails.append(f"manifest.json: {exc}")
    return fails


def lint(site: str) -> list[str]:
    """megawob.lint's errors for the site (about 15 s): the design pattern, manifest drift, __env calls, ..."""
    out = _megawob("megawob.lint", "--no-ruff", "--errors", "--site", site, timeout=300)
    if out.returncode == 0:
        return []
    lines = [line.strip() for line in out.stdout.splitlines() if line.strip() and "findings," not in line]
    return [f"megawob.lint: {line}"[:400] for line in lines[:10]] or [f"megawob.lint exited {out.returncode}: "
                                                                       f"{out.stderr[-300:]}"]


def oracle(site: str, seeds: int, viewports: list[str], fail_fast: int = 3) -> list[str]:
    """megawob.check on the site: `seeds` plain seeds plus the parameter sweep in each viewport; one line per
    failure."""
    out = _megawob("megawob.check", "--site", site, "--seeds", str(seeds), "--jobs", str(min(len(viewports), 3)),
                   "--viewports", *viewports, "--fail-fast", str(fail_fast))
    stdout = "\n" + out.stdout
    if "\ncell " not in stdout:
        lines = (out.stderr or out.stdout).strip().splitlines()
        error = next((x.strip() for x in reversed(lines) if "Error" in x and not x.strip().startswith("at ")),
                     lines[-1] if lines else f"exit {out.returncode}")
        return [f"megawob.check crashed: {error}"[:400]]
    fails, cell = [], ""
    for line in stdout.split("\ncell ", 1)[1].splitlines()[1:]:
        if line.startswith("    - "):
            fails.append(f"megawob.check {cell}: {line[6:].strip()}"[:400])
        elif line.strip() and not line.startswith(" "):
            cell = line.split()[0].rsplit("@", 1)[-1]  # the viewport
    return fails[:20]


def quick(site: str) -> list[str]:
    """The build agent's check: lint, then the oracle at the smallest viewport, then at a middle and the largest."""
    return lint(site) or oracle(site, 1, QUICK_VIEWPORTS[:1]) or oracle(site, 1, QUICK_VIEWPORTS[1:])


def full(site: str, task: str, seeds: int = 5) -> list[str]:
    """The gate: files and syntax, the lint, then the oracle at every training viewport."""
    return files(site, task) or lint(site) or oracle(site, seeds, TRAIN_VIEWPORTS)


def index(site: str) -> None:
    """Add the site's widget to html/scenarios.json (the checker fails a widget missing from it; megawob.scenarios
    locks the index, so parallel builds can call this)."""
    out = _megawob("megawob.scenarios", "--site", site, timeout=300)
    if out.returncode:
        raise RuntimeError(f"megawob.scenarios --site {site}: {(out.stderr or out.stdout)[-400:]}")
