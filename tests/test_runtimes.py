"""The parts of the runtime adapters that need no model: config generation, the write rule, session cleanup."""
import asyncio
import shutil
import tomllib

import pytest

from megawob_harness.jsonio import fill, parse_json
from megawob_harness.runtime import SessionSpec


def test_codex_mcp_overrides_are_valid_toml():
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    for override in codex.mcp_overrides({"harness": ["python", "-m", "x", "spec.json"]}):
        tomllib.loads(override.replace("mcp_servers.harness.", "", 1))


def test_codex_sandbox_writes_only_in_the_workspace(monkeypatch, tmp_path):
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    monkeypatch.setattr(codex, "Codex", lambda config: config)  # the config it would start the app-server with
    overrides = codex.CodexRuntime().codex((), tmp_path).config_overrides
    for setting in ("exclude_slash_tmp", "exclude_tmpdir_env_var"):  # workspace-write also allows both by default
        assert f"sandbox_workspace_write.{setting}=true" in overrides
    for override in overrides:
        tomllib.loads(override)


def test_codex_runs_in_a_home_with_only_the_login(tmp_path, monkeypatch):
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    user = tmp_path / "user"
    user.mkdir()
    for name in ("auth.json", "config.toml", "AGENTS.md"):
        (user / name).write_text("x")
    monkeypatch.setenv("CODEX_HOME", str(user))
    home = codex.isolated_home()
    assert sorted(p.name for p in home.iterdir()) == ["auth.json", "config.toml"]
    assert (home / "auth.json").resolve() == user / "auth.json" and (home / "config.toml").read_text() == ""
    shutil.rmtree(home)


def test_a_codex_session_that_fails_to_start_removes_its_home(tmp_path, monkeypatch):
    codex = pytest.importorskip("megawob_harness.runtime.codex")
    homes = []
    monkeypatch.setattr(codex, "isolated_home", lambda: homes.append(tmp_path / "home") or homes[-1])
    (tmp_path / "home").mkdir()

    def no_app_server(*args):
        raise RuntimeError("codex app-server did not start")

    monkeypatch.setattr(codex.CodexRuntime, "codex", no_app_server)
    with pytest.raises(RuntimeError), codex.CodexRuntime().session(SessionSpec(tmp_path)):
        pass
    assert homes and not homes[0].exists()


def test_claude_writes_are_allowed_only_in_the_workspace():
    claude = pytest.importorskip("megawob_harness.runtime.claude")
    # Edit(/**) is every write under the session's folder; a bare Write or Edit would allow writes anywhere.
    assert "Edit(/**)" in claude.ALLOWED and not {"Write", "Edit"} & set(claude.ALLOWED)


def test_parse_json_accepts_wrapped_objects():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    assert parse_json('Here:\n```json\n{"a": 1}\n```', schema) == {"a": 1}
    with pytest.raises(ValueError):
        parse_json('{"a": "x"}', schema)


def test_fill_is_one_pass_and_leaves_other_braces():
    assert fill("{a} {b} {c} {}", a="{b}", b='{"x": 1}') == '{b} {"x": 1} {c} {}'


def test_claude_sessions_close_their_event_loops(monkeypatch, tmp_path):
    claude = pytest.importorskip("megawob_harness.runtime.claude")
    runners, starts = [], 0

    class Runner(asyncio.Runner):
        def __init__(self):
            super().__init__()
            self.closed = False
            runners.append(self)

        def close(self):
            super().close()
            self.closed = True

    async def connect(self):
        nonlocal starts
        starts += 1
        if starts % 2:
            raise ConnectionError("the CLI did not start")

    async def disconnect(self):
        pass

    monkeypatch.setattr(claude.asyncio, "Runner", Runner)
    monkeypatch.setattr(claude.ClaudeSDKClient, "connect", connect)
    monkeypatch.setattr(claude.ClaudeSDKClient, "disconnect", disconnect)
    for _ in range(4):  # half of them fail to start
        try:
            with claude.ClaudeRuntime().session(SessionSpec(tmp_path)):
                pass
        except ConnectionError:
            pass
    assert len(runners) == 4 and all(r.closed for r in runners)
