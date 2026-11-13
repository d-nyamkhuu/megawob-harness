"""The agent's tools: the domain's tools plus `check`, served over MCP (stdio) in a process of their own.

Both runtimes start this server outside the agent's sandbox, so a tool can run a browser or a test suite that the
agent itself could not. The agent can only call the tools; the ledger they write lives outside its workspace.

    python -m agent_harness.tools <spec.json>

The spec names the domain (import path and config), the job, the workspace, the state directory, the server name and
the harness's working directory, which the server changes to (runtimes start it in the agent's workspace, and a
domain's config may hold relative paths).
`build(spec)` returns the same functions in-process (the replay runtime and the tests call them directly).
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from .checks import Ledger, check_tool
from .domain import load_domain

__all__ = ["Image", "build", "main", "write_spec"]


def write_spec(path: Path, *, domain: str, config: dict, job: dict, workspace: Path, state: Path,
               ignore: tuple[str, ...], server: str = "harness") -> Path:
    path.write_text(json.dumps({"domain": domain, "config": config, "job": job, "workspace": str(workspace),
                                "state": str(state), "ignore": list(ignore), "server": server, "cwd": os.getcwd()}))
    return path


def build(spec: dict) -> dict[str, Callable]:
    domain = load_domain(spec["domain"], spec["config"])
    job, workspace = spec["job"], Path(spec["workspace"])
    tools = {fn.__name__: fn for fn in domain.tools(job)}
    tools["check"] = check_tool(lambda: domain.quick_check(job), workspace, Ledger(Path(spec["state"])),
                                spec.get("ignore", ()))
    return tools


def _serve(spec: dict) -> MCPServer:
    server = MCPServer(spec.get("server", "harness"))
    pool = ThreadPoolExecutor(1)  # one worker thread: Playwright's sync API must stay on one thread, off the loop

    for name, fn in build(spec).items():
        @functools.wraps(fn)
        async def call(*args, _fn=fn, **kwargs):
            return await asyncio.get_running_loop().run_in_executor(pool, functools.partial(_fn, *args, **kwargs))

        server.add_tool(call, name=name, description=fn.__doc__)
    return server


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    spec = json.loads(Path(argv[0]).read_text())
    # Runtimes start MCP servers in the agent's workspace. The domain's relative paths and its module are the
    # harness's, so work from the harness's directory and import from it, as the harness itself does.
    os.chdir(spec["cwd"])
    sys.path.insert(0, spec["cwd"])
    _serve(spec).run()


if __name__ == "__main__":
    main()
