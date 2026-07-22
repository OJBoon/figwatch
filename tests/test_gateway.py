"""Tests for figwatch.gateway — cc-switch / custom-gateway detection and wiring.

All detection is driven by injected ``env`` dicts or ``tmp_path`` files so the
tests never depend on the developer's real ~/.claude/settings.json.
"""

import json

from figwatch.gateway import (
    gateway_info,
    gateway_subprocess_env,
    read_claude_env,
)
from figwatch.providers.ai import make_provider
from figwatch.providers.ai.claude_cli import ClaudeCLIProvider

GATEWAY_ENV = {
    "ANTHROPIC_BASE_URL": "https://gateway.example.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "tok-123",
    "ANTHROPIC_MODEL": "company-model",
    "ANTHROPIC_SMALL_FAST_MODEL": "company-fast",
}


# ── gateway_info ──────────────────────────────────────────────────────

def test_gateway_info_detects_active_gateway():
    gw = gateway_info(GATEWAY_ENV)
    assert gw is not None
    assert gw["base_url"] == "https://gateway.example.com/anthropic"
    assert gw["host"] == "gateway.example.com"
    assert gw["model"] == "company-model"
    assert gw["small_fast_model"] == "company-fast"


def test_gateway_info_accepts_api_key_credential():
    env = {"ANTHROPIC_BASE_URL": "https://gw.example.com", "ANTHROPIC_API_KEY": "sk-1"}
    assert gateway_info(env) is not None


def test_gateway_info_none_without_base_url():
    assert gateway_info({"ANTHROPIC_AUTH_TOKEN": "tok"}) is None


def test_gateway_info_none_without_token():
    assert gateway_info({"ANTHROPIC_BASE_URL": "https://gw.example.com"}) is None


def test_gateway_info_none_when_empty():
    assert gateway_info({}) is None


def test_gateway_info_ignores_blank_values():
    env = {"ANTHROPIC_BASE_URL": "  ", "ANTHROPIC_AUTH_TOKEN": "  "}
    assert gateway_info(env) is None


def test_gateway_info_model_optional():
    env = {"ANTHROPIC_BASE_URL": "https://gw.example.com", "ANTHROPIC_AUTH_TOKEN": "t"}
    gw = gateway_info(env)
    assert gw is not None
    assert gw["model"] == ""


# ── read_claude_env ───────────────────────────────────────────────────

def test_read_claude_env_from_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"env": GATEWAY_ENV, "other": 1}))
    assert read_claude_env(str(p)) == GATEWAY_ENV


def test_read_claude_env_missing_file(tmp_path):
    assert read_claude_env(str(tmp_path / "nope.json")) == {}


def test_read_claude_env_malformed(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    assert read_claude_env(str(p)) == {}


def test_read_claude_env_no_env_block(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "dark"}))
    assert read_claude_env(str(p)) == {}


# ── gateway_subprocess_env ────────────────────────────────────────────

def test_gateway_subprocess_env_returns_set_keys_only():
    out = gateway_subprocess_env(GATEWAY_ENV)
    assert out == GATEWAY_ENV  # all four keys are set
    assert "ANTHROPIC_API_KEY" not in out  # absent key not fabricated


def test_gateway_subprocess_env_empty_without_gateway():
    assert gateway_subprocess_env({"ANTHROPIC_MODEL": "x"}) == {}


# ── provider wiring ───────────────────────────────────────────────────

def test_claude_cli_model_id_uses_gateway_model():
    p = ClaudeCLIProvider("sonnet", "/bin/claude", gateway={"model": "company-model"})
    assert p.model_id == "company-model"


def test_claude_cli_model_id_falls_back_to_alias_without_gateway():
    p = ClaudeCLIProvider("sonnet", "/bin/claude")
    assert p.model_id == "sonnet"


def test_claude_cli_model_id_falls_back_when_gateway_has_no_model():
    p = ClaudeCLIProvider("sonnet", "/bin/claude", gateway={"model": ""})
    assert p.model_id == "sonnet"


def test_make_provider_forwards_gateway_to_cli():
    gw = {"model": "company-model", "host": "gw"}
    p = make_provider("sonnet", "/bin/claude", gateway=gw)
    assert isinstance(p, ClaudeCLIProvider)
    assert p._gateway is gw
    assert p.model_id == "company-model"


def test_make_provider_cli_defaults_to_no_gateway():
    p = make_provider("sonnet", "/bin/claude")
    assert isinstance(p, ClaudeCLIProvider)
    assert p._gateway is None


# ── command construction (model flag) ─────────────────────────────────

class _FakeResult:
    stdout = b"reply text"
    stderr = b""
    returncode = 0


def _capture_cmd(monkeypatch):
    import figwatch.handlers as handlers
    import figwatch.providers.ai.claude_cli as cc

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeResult()

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    # Keep the subprocess env hermetic (don't read the real settings file).
    monkeypatch.setattr(handlers, "subprocess_env", lambda: {})
    return captured


def test_cli_omits_model_flag_in_gateway_mode(monkeypatch):
    captured = _capture_cmd(monkeypatch)
    p = ClaudeCLIProvider("sonnet", "/bin/claude", gateway={"model": "company-model"})
    out = p.call("hi", None)
    assert out == "reply text"
    assert "--model" not in captured["cmd"]


def test_cli_includes_model_flag_in_personal_mode(monkeypatch):
    captured = _capture_cmd(monkeypatch)
    p = ClaudeCLIProvider("sonnet", "/bin/claude")
    p.call("hi", None)
    cmd = captured["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
