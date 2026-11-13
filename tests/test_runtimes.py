"""The parts of the runtime adapters that need no model: config generation, the write guard, the MCP server."""
import asyncio
import os
import tomllib

import pytest

from megawob_harness.jsonio import fill, parse_json


def test_codex_mcp_overrides_are_valid_toml():
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    for override in codex.mcp_overrides({"harness": ["python", "-m", "x", "spec.json"]}):
        tomllib.loads(override.replace("mcp_servers.harness.", "", 1))


def test_codex_runs_in_a_home_with_only_the_login(tmp_path, monkeypatch):
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    user = tmp_path / "user"
    user.mkdir()
    for name in ("auth.json", "config.toml", "AGENTS.md"):
        (user / name).write_text("x")
    monkeypatch.setenv("CODEX_HOME", str(user))
    with codex.isolated_home() as home:
        assert sorted(p.name for p in home.iterdir()) == ["auth.json", "config.toml"]
        assert (home / "auth.json").resolve() == user / "auth.json" and (home / "config.toml").read_text() == ""
    assert not home.exists()


def test_claude_write_guard(tmp_path):
    claude = pytest.importorskip("megawob_harness.runtime.claude")
    assert claude.inside(tmp_path, "a/b.txt") and claude.inside(tmp_path, str(tmp_path / "x"))
    assert not claude.inside(tmp_path, "../escape") and not claude.inside(tmp_path, "/etc/passwd")
    guard = claude.write_guard(tmp_path)
    denied = asyncio.run(guard({"tool_input": {"file_path": str(tmp_path.parent / "outside.txt")}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(guard({"tool_input": {"file_path": "ok.txt"}}, None, None)) == {}


def test_parse_json_accepts_wrapped_objects():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    assert parse_json('Here:\n```json\n{"a": 1}\n```', schema) == {"a": 1}
    with pytest.raises(ValueError):
        parse_json('{"a": "x"}', schema)


def test_fill_is_one_pass_and_leaves_other_braces():
    assert fill("{a} {b} {c} {}", a="{b}", b='{"x": 1}') == '{b} {"x": 1} {c} {}'


def test_claude_session_loops_release_their_file_descriptors():
    from megawob_harness.runtime.claude import _Loop

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        _Loop().close()
    assert len(os.listdir("/proc/self/fd")) - before < 5
