"""The parts of the runtime adapters that need no model: config generation, the write guard, the MCP server."""
import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from agent_harness.checks import StopGate
from agent_harness.jsonio import parse_json
from agent_harness.runtime.base import SessionSpec
from agent_harness.tools import write_spec


def test_codex_overrides_are_valid_toml(tmp_path):
    codex = pytest.importorskip("agent_harness.runtime.codex")
    spec = SessionSpec(cwd=tmp_path, stop_gate=StopGate(tmp_path, tmp_path / "s", ["NOTES.md"]))
    command = codex.stop_hook_command(spec)
    key, _, value = codex.stop_hook_override(command).partition("=")
    hooks = tomllib.loads(f"v = {value}")["v"]
    assert key == "hooks.Stop" and hooks[0]["hooks"][0] == {"type": "command", "command": command, "timeout": 120}
    assert "--ignore NOTES.md" in command
    trust = tomllib.loads(codex.trust_entry("/<session-flags>/config.toml:stop:0:0", "sha256:ab"))
    assert trust["hooks"]["state"]["/<session-flags>/config.toml:stop:0:0"] == {"trusted_hash": "sha256:ab"}
    for override in codex.mcp_overrides({"harness": ["python", "-m", "x", "spec.json"]}):
        tomllib.loads(override.replace("mcp_servers.harness.", "", 1))


def test_claude_write_guard(tmp_path):
    claude = pytest.importorskip("agent_harness.runtime.claude")
    assert claude.inside(tmp_path, "a/b.txt") and claude.inside(tmp_path, str(tmp_path / "x"))
    assert not claude.inside(tmp_path, "../escape") and not claude.inside(tmp_path, "/etc/passwd")
    guard = claude.write_guard(tmp_path)
    denied = asyncio.run(guard({"tool_input": {"file_path": str(tmp_path.parent / "ledger")}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(guard({"tool_input": {"file_path": "ok.txt"}}, None, None)) == {}


def test_parse_json_accepts_wrapped_objects():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    assert parse_json('Here:\n```json\n{"a": 1}\n```', schema) == {"a": 1}
    with pytest.raises(ValueError):
        parse_json('{"a": "x"}', schema)


def test_the_tool_server_serves_domain_tools_and_check_over_mcp(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tests.toy_domain import Toy
    toy, job = Toy(root=str(tmp_path / "ws")), {"id": "j"}
    toy.prepare(job)
    (toy.workspace(job) / "answer.txt").write_text("42\n")
    spec = write_spec(tmp_path / "spec.json", domain="tests.toy_domain:Toy", config=toy.config, job=job,
                      workspace=toy.workspace(job), state=tmp_path / "state", ignore=toy.ignore)

    async def talk():
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_harness.tools", str(spec)],
                                       cwd=str(Path(__file__).parents[1]))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            peek = await session.call_tool("peek", {})
            check = await session.call_tool("check", {})
            return names, peek.content[0].text, check.content[0].text

    names, peek, check = asyncio.run(talk())
    assert names == ["check", "peek"] and peek == "42\n" and check.startswith("check passed. check_token: ")
    assert json.loads((tmp_path / "state" / "checks.jsonl").read_text())["passed"]


def test_the_tool_server_resolves_relative_paths_from_the_harness_not_the_workspace(tmp_path, monkeypatch):
    """Runtimes start MCP servers in the agent's workspace; a domain configured with a relative root must still
    find it (a live run's check() hashed a folder that did not exist)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tests.toy_domain import Toy
    repo = Path(__file__).parents[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(repo))
    toy, job = Toy(root="ws"), {"id": "j"}
    toy.prepare(job)
    (toy.workspace(job) / "answer.txt").write_text("42\n")
    spec = write_spec(tmp_path / "spec.json", domain="tests.toy_domain:Toy", config=toy.config, job=job,
                      workspace=toy.workspace(job).resolve(), state=tmp_path / "state", ignore=toy.ignore)

    async def talk():
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_harness.tools", str(spec)],
                                       cwd=str(toy.workspace(job).resolve()), env=dict(os.environ))
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return (await session.call_tool("check", {})).content[0].text

    assert asyncio.run(talk()).startswith("check passed")
