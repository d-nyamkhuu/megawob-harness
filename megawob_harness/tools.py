"""The build agent's tools for one site, `render` and `check`, served over MCP (stdio) in a process of their own.

Both runtimes start this server outside the agent's sandbox, so it can run Chromium and the checker while the agent
itself can only write in the site folder.

    python -m megawob_harness.tools <spec.json>    # spec: site, task, scenario, image

`build(spec)` returns the same functions in-process (the replay runtime and the tests call them directly).
"""
from __future__ import annotations

import asyncio
import functools
import io
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from . import gate
from .render import shots


def build(spec: dict) -> dict[str, Callable]:
    def render(seed: int = 0, view: str = "design", crop: list[int] | None = None, popups: str | None = None,
               params: dict[str, str] | None = None, play: int = 0) -> list:
        """Render the site and return the image plus any page, console and layout errors.

        view: "design" (the design image beside the render at the image's size, both scaled to 0.6), "small"
        (renders at 1280x720 and 1024x640, to check the fit) or "modes" (a 2x2 grid at 1024x640: the instruction
        as header, panel and toast, and hidden with finish by message, so no end control).
        crop: [x, y, w, h] in design-image pixels: that region of the design image and of the render at full size.
        popups: open this overlay kind from the start (cookie, toast, chat, promo, banner).
        params: randomization parameters, e.g. {"instructionDisplay": "panel", "startState": "scene",
        "pageFaults": "revert"}.
        play: first follow the oracle for this many actions (to see a checker failure mid-episode); lists them.
        seed: content and the per-episode frame choices (end-control placement and label, decoys) vary by seed.
        """
        request = {"seed": seed, "view": view, "crop": crop, "popups": popups,
                   "params": {**(params or {}), "play": play}}
        try:  # a browser of its own, gone when this returns
            ((img, errors, notes),) = shots(spec["site"], spec["task"], spec["scenario"], Path(spec["image"]),
                                            [request])
        except RuntimeError as exc:
            return f"ERROR {exc}"
        buf = io.BytesIO()
        img.save(buf, "PNG")
        text = "\n".join([*(f"PLAY {n}" for n in notes), *(f"ERROR {e}" for e in errors)]) or "no page errors"
        return [Image(data=buf.getvalue(), format="png"), text]

    def check() -> str:
        """The static lint, then the oracle checker (1 seed plus its parameter sweep at 1024x640, then 1366x768 and
        1536x864): up to 3 failures per viewport, each with its seed and parameters, or "check passed". About 1-2
        minutes. The final gate runs the same checks with more seeds at all five training viewports."""
        try:
            failures = gate.quick(spec["site"])
        except Exception as exc:  # a crashing check is a failure the agent should see, not a protocol error
            failures = [f"the check crashed: {type(exc).__name__}: {exc}"[:400]]
        return "\n".join(f"FAIL {f}" for f in failures) if failures else "check passed"

    return {"render": render, "check": check}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    spec = json.loads(Path(argv[0]).read_text())
    server = MCPServer("megawob")
    pool = ThreadPoolExecutor(1)  # the blocking tools run off the event loop, one at a time

    for name, fn in build(spec).items():
        @functools.wraps(fn)
        async def call(*args, _fn=fn, **kwargs):
            return await asyncio.get_running_loop().run_in_executor(pool, functools.partial(_fn, *args, **kwargs))

        server.add_tool(call, name=name, description=fn.__doc__)
    server.run()


if __name__ == "__main__":
    main()
