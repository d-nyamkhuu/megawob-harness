"""The parts of the runtime adapters that need no model: config generation, the write guard, the MCP server."""
import asyncio
import tomllib

import pytest

from megawob_harness.checks import StopGate
from megawob_harness.jsonio import parse_json
from megawob_harness.runtime.base import SessionSpec


def test_codex_overrides_are_valid_toml(tmp_path):
    codex = pytest.importorskip("megawob_harness.runtime.codex")
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
    claude = pytest.importorskip("megawob_harness.runtime.claude")
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
