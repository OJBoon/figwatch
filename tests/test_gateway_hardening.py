"""Exhaustive hardening tests for figwatch.gateway multi-source detection.

Complements tests/test_gateway.py. Covers the full detection-source walk
(precedence, graceful degradation), the Option-B manual fallback, the
``gateway_api_config`` credential mapping, ``make_provider`` gateway routing
(and the server backward-compat path), ``AnthropicProvider`` field storage, and
``cc_switch_installed`` messaging.

Every source is exercised hermetically: the module-level path constants are
monkeypatched to point at ``tmp_path`` files (referenced lazily by the source
loaders, so patching the module global is enough), ``os.environ`` is scrubbed of
recognised ``ANTHROPIC_*`` keys, and ``gateway_api_config`` is monkeypatched for
the ``make_provider`` tests. No real network, no real filesystem outside tmp.
"""

import json
import os
import stat

import pytest

import figwatch.gateway as gw
from figwatch.gateway import (
    _has_gateway,
    _json_file_env,
    _manual_gateway_env,
    _os_environ_env,
    cc_switch_installed,
    effective_gateway_env,
    gateway_api_config,
    gateway_info,
    read_manual_gateway,
    write_manual_gateway,
)
from figwatch.providers.ai import CLAUDE_API_MODELS, make_provider, reset_limiters
from figwatch.providers.ai.anthropic import AnthropicProvider

GATEWAY_ENV = {
    "ANTHROPIC_BASE_URL": "https://gateway.example.com/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "tok-123",
    "ANTHROPIC_MODEL": "company-model",
    "ANTHROPIC_SMALL_FAST_MODEL": "company-fast",
}


# ── Source isolation fixture ───────────────────────────────────────────
# Points every detection source at a distinct (initially non-existent) tmp
# path and clears recognised ANTHROPIC_* vars from os.environ, so each test
# populates exactly one source. The source loaders resolve these module
# globals at call time, so setattr on the module is sufficient.

@pytest.fixture
def sources(monkeypatch, tmp_path):
    paths = {
        "claude_settings": tmp_path / "claude_settings.json",
        "claude_settings_local": tmp_path / "claude_settings_local.json",
        "claude_config_xdg": tmp_path / "xdg_settings.json",
        "claude_json": tmp_path / "claude.json",
        "manual": tmp_path / "figwatch_gateway.json",
    }
    monkeypatch.setattr(gw, "CLAUDE_SETTINGS_PATH", str(paths["claude_settings"]))
    monkeypatch.setattr(gw, "CLAUDE_SETTINGS_LOCAL_PATH", str(paths["claude_settings_local"]))
    monkeypatch.setattr(gw, "CLAUDE_CONFIG_XDG_PATH", str(paths["claude_config_xdg"]))
    monkeypatch.setattr(gw, "CLAUDE_JSON_PATH", str(paths["claude_json"]))
    monkeypatch.setattr(gw, "MANUAL_GATEWAY_PATH", str(paths["manual"]))
    for k in gw.GATEWAY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return paths


@pytest.fixture(autouse=True)
def _reset_limiters():
    reset_limiters()
    yield
    reset_limiters()


def _write_env_block(path, env):
    """Write ``{"env": <env>}`` (cc-switch canonical layout) to a file."""
    path.write_text(json.dumps({"env": env, "unrelated": {"x": 1}}))


# ── Scenario 1: gateway in a settings.json env block ───────────────────

def test_detected_from_claude_settings_env_block(sources):
    _write_env_block(sources["claude_settings"], GATEWAY_ENV)
    gw_info = gateway_info()
    assert gw_info is not None
    assert gw_info["base_url"] == "https://gateway.example.com/anthropic"
    assert gw_info["host"] == "gateway.example.com"
    assert gw_info["model"] == "company-model"
    assert gw_info["small_fast_model"] == "company-fast"


# ── Scenario 2: only settings.local.json defines a gateway ─────────────

def test_detected_from_settings_local_when_primary_absent(sources):
    # Primary settings has no gateway at all (missing file); local defines one.
    _write_env_block(sources["claude_settings_local"], GATEWAY_ENV)
    assert gateway_info() is not None
    assert gateway_info()["host"] == "gateway.example.com"


def test_detected_from_settings_local_when_primary_has_no_gateway(sources):
    # Primary settings file exists but carries no gateway keys.
    sources["claude_settings"].write_text(json.dumps({"env": {"THEME": "dark"}}))
    _write_env_block(sources["claude_settings_local"], GATEWAY_ENV)
    assert gateway_info() is not None


# ── Scenario 3: only ~/.config/claude/settings.json (XDG) ──────────────

def test_detected_from_xdg_config(sources):
    _write_env_block(sources["claude_config_xdg"], GATEWAY_ENV)
    assert gateway_info() is not None
    assert gateway_info()["model"] == "company-model"


# ── Scenario 4: only ~/.claude.json, TOP-LEVEL ANTHROPIC_* keys ────────

def test_detected_from_claude_json_top_level_keys(sources):
    # Not nested under "env" — the ~/.claude.json layout.
    sources["claude_json"].write_text(json.dumps(dict(GATEWAY_ENV)))
    gw_info = gateway_info()
    assert gw_info is not None
    assert gw_info["host"] == "gateway.example.com"
    assert gw_info["model"] == "company-model"


# ── Scenario 5: only os.environ ────────────────────────────────────────

def test_detected_from_os_environ(sources, monkeypatch):
    for k, v in GATEWAY_ENV.items():
        monkeypatch.setenv(k, v)
    gw_info = gateway_info()
    assert gw_info is not None
    assert gw_info["host"] == "gateway.example.com"


# ── Scenario 6: only manual ~/.figwatch/gateway.json ───────────────────

def test_detected_from_manual_fallback(sources):
    sources["manual"].write_text(json.dumps({
        "base_url": "https://gw.manual.local/anthropic",
        "auth_token": "man-tok",
        "model": "manual-model",
    }))
    gw_info = gateway_info()
    assert gw_info is not None
    assert gw_info["base_url"] == "https://gw.manual.local/anthropic"
    assert gw_info["model"] == "manual-model"


# ── Scenario 7: nothing anywhere → None (Docker/server backward-compat) ─

def test_no_gateway_anywhere_returns_none(sources):
    assert effective_gateway_env() == {}
    assert gateway_info() is None
    assert gateway_api_config() is None


# ── Scenario 8: malformed JSON in a source → skipped, walk continues ───

def test_malformed_primary_source_is_skipped(sources):
    sources["claude_settings"].write_text("{ this is not json")
    _write_env_block(sources["claude_settings_local"], GATEWAY_ENV)
    # Malformed primary must not raise and must not block the local source.
    assert gateway_info() is not None
    assert gateway_info()["host"] == "gateway.example.com"


def test_malformed_manual_source_is_skipped(sources):
    sources["manual"].write_text("}{ broken")
    # No other source → graceful None, not an exception.
    assert gateway_info() is None


# ── Scenario 9: base_url but no token → not a gateway ──────────────────

def test_base_url_without_token_is_not_gateway_injected():
    assert gateway_info({"ANTHROPIC_BASE_URL": "https://gw.example.com"}) is None


def test_base_url_without_token_is_not_gateway_from_source(sources):
    _write_env_block(sources["claude_settings"], {"ANTHROPIC_BASE_URL": "https://gw.example.com"})
    assert gateway_info() is None
    assert gateway_api_config() is None


# ── Scenario 10: token but no base_url → not a gateway ─────────────────

def test_token_without_base_url_is_not_gateway_injected():
    assert gateway_info({"ANTHROPIC_AUTH_TOKEN": "tok"}) is None


def test_token_without_base_url_is_not_gateway_from_source(sources):
    _write_env_block(sources["claude_settings"], {"ANTHROPIC_AUTH_TOKEN": "tok"})
    assert gateway_info() is None


# ── Scenario 11: empty / whitespace values treated as absent ───────────

def test_whitespace_values_treated_as_absent_injected():
    assert gateway_info({"ANTHROPIC_BASE_URL": "   ", "ANTHROPIC_AUTH_TOKEN": "\t"}) is None


def test_whitespace_values_treated_as_absent_from_source(sources):
    # _json_file_env drops blank string values, so this source yields nothing.
    _write_env_block(sources["claude_settings"], {
        "ANTHROPIC_BASE_URL": "  ",
        "ANTHROPIC_AUTH_TOKEN": "   ",
    })
    assert _json_file_env(str(sources["claude_settings"])) == {}
    assert gateway_info() is None


def test_whitespace_values_treated_as_absent_in_os_environ(sources, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "   ")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "  ")
    assert _os_environ_env() == {}
    assert gateway_info() is None


# ── Scenario 12: file exists but {} / no env → nothing from that source ─

def test_empty_object_file_yields_nothing(sources):
    sources["claude_settings"].write_text("{}")
    assert _json_file_env(str(sources["claude_settings"])) == {}
    assert gateway_info() is None


def test_file_with_no_env_block_yields_nothing(sources):
    sources["claude_settings"].write_text(json.dumps({"theme": "dark", "permissions": {}}))
    assert _json_file_env(str(sources["claude_settings"])) == {}
    assert gateway_info() is None


# ── Scenario 13: api_key credential → gateway_api_config returns api_key ─

def test_gateway_api_config_uses_api_key_when_no_auth_token_injected():
    env = {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "ANTHROPIC_API_KEY": "sk-abc",
        "ANTHROPIC_MODEL": "m",
    }
    cfg = gateway_api_config(env)
    assert cfg is not None
    assert cfg["api_key"] == "sk-abc"
    assert "auth_token" not in cfg
    assert cfg["base_url"] == "https://gw.example.com"
    assert cfg["model"] == "m"


def test_gateway_api_config_prefers_auth_token_over_api_key():
    env = {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_API_KEY": "sk-abc",
    }
    cfg = gateway_api_config(env)
    assert cfg["auth_token"] == "tok"
    assert "api_key" not in cfg


def test_gateway_api_config_uses_api_key_from_source(sources):
    _write_env_block(sources["claude_settings"], {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "ANTHROPIC_API_KEY": "sk-src",
    })
    cfg = gateway_api_config()
    assert cfg is not None
    assert cfg["api_key"] == "sk-src"
    assert "auth_token" not in cfg


def test_gateway_api_config_model_empty_when_unset():
    env = {"ANTHROPIC_BASE_URL": "https://gw.example.com", "ANTHROPIC_AUTH_TOKEN": "t"}
    cfg = gateway_api_config(env)
    assert cfg["model"] == ""


# ── Scenario 14: precedence — higher source wins ───────────────────────

def _env_with_model(model):
    return {
        "ANTHROPIC_BASE_URL": f"https://{model}.example.com",
        "ANTHROPIC_AUTH_TOKEN": f"tok-{model}",
        "ANTHROPIC_MODEL": model,
    }


def test_precedence_settings_beats_env_and_manual(sources, monkeypatch):
    _write_env_block(sources["claude_settings"], _env_with_model("primary"))
    for k, v in _env_with_model("fromenv").items():
        monkeypatch.setenv(k, v)
    sources["manual"].write_text(json.dumps({
        "base_url": "https://manual.example.com",
        "auth_token": "tok-manual",
        "model": "manual",
    }))
    assert gateway_info()["model"] == "primary"
    assert effective_gateway_env()["ANTHROPIC_MODEL"] == "primary"


def test_precedence_settings_local_beats_xdg_and_claude_json(sources):
    _write_env_block(sources["claude_settings_local"], _env_with_model("local"))
    _write_env_block(sources["claude_config_xdg"], _env_with_model("xdg"))
    sources["claude_json"].write_text(json.dumps(_env_with_model("claudejson")))
    assert gateway_info()["model"] == "local"


def test_precedence_env_beats_manual_when_files_absent(sources, monkeypatch):
    for k, v in _env_with_model("fromenv").items():
        monkeypatch.setenv(k, v)
    sources["manual"].write_text(json.dumps({
        "base_url": "https://manual.example.com",
        "auth_token": "tok-manual",
        "model": "manual",
    }))
    assert gateway_info()["model"] == "fromenv"


def test_precedence_skips_incomplete_higher_source(sources, monkeypatch):
    # Primary has base_url but no token (incomplete) → skipped; env wins.
    _write_env_block(sources["claude_settings"], {"ANTHROPIC_BASE_URL": "https://gw.example.com"})
    for k, v in _env_with_model("fromenv").items():
        monkeypatch.setenv(k, v)
    assert gateway_info()["model"] == "fromenv"


# ── Scenario 15: base_url with path + http preserved ───────────────────

def test_http_base_url_with_path_preserved():
    env = {
        "ANTHROPIC_BASE_URL": "http://llm-gw.jd.local/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_MODEL": "Claude-Opus-4.8-joybuilder",
    }
    gw_info = gateway_info(env)
    assert gw_info["base_url"] == "http://llm-gw.jd.local/anthropic"
    assert gw_info["host"] == "llm-gw.jd.local"
    cfg = gateway_api_config(env)
    assert cfg["base_url"] == "http://llm-gw.jd.local/anthropic"
    assert cfg["model"] == "Claude-Opus-4.8-joybuilder"


# ── Scenario 16: unreadable source → graceful ─────────────────────────

def test_unreadable_source_is_a_directory_graceful(sources, monkeypatch, tmp_path):
    # Point the primary settings path at a directory: open() raises, which must
    # be caught so the walk continues to the (gateway-bearing) local source.
    a_dir = tmp_path / "settings_is_a_dir"
    a_dir.mkdir()
    monkeypatch.setattr(gw, "CLAUDE_SETTINGS_PATH", str(a_dir))
    assert _json_file_env(str(a_dir)) == {}  # graceful, no raise
    _write_env_block(sources["claude_settings_local"], GATEWAY_ENV)
    assert gateway_info() is not None
    assert gateway_info()["host"] == "gateway.example.com"


def test_unreadable_source_no_permission_graceful(sources, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root: permission bits do not restrict reads")
    p = tmp_path / "noperm.json"
    p.write_text(json.dumps({"env": GATEWAY_ENV}))
    os.chmod(p, 0o000)
    try:
        # Confirm the OS actually denies the read before asserting graceful.
        try:
            with open(p):
                pass
            pytest.skip("filesystem ignores permission bits; cannot test unreadable")
        except PermissionError:
            pass
        assert _json_file_env(str(p)) == {}
    finally:
        os.chmod(p, 0o600)


# ── Scenario 17: make_provider gateway routing + server backward-compat ─

def test_make_provider_gateway_builds_anthropic_over_gateway(monkeypatch):
    cfg = {
        "base_url": "http://llm-gw.jd.local/anthropic",
        "model": "Claude-Opus-4.8-joybuilder",
        "auth_token": "gw-tok",
    }
    monkeypatch.setattr(gw, "gateway_api_config", lambda env=None: cfg)
    p = make_provider("opus", "api", gateway={"host": "llm-gw.jd.local", "model": "x"})
    assert isinstance(p, AnthropicProvider)
    assert p.model_id == "Claude-Opus-4.8-joybuilder"
    assert p._base_url == "http://llm-gw.jd.local/anthropic"
    assert p._auth_token == "gw-tok"
    assert p._max_tokens == 8192


def test_make_provider_gateway_api_key_variant(monkeypatch):
    cfg = {"base_url": "https://gw.example.com", "model": "m", "api_key": "sk-gw"}
    monkeypatch.setattr(gw, "gateway_api_config", lambda env=None: cfg)
    p = make_provider("opus", "api", gateway={"host": "gw"})
    assert p._base_url == "https://gw.example.com"
    assert p._auth_token is None
    assert p._api_key == "sk-gw"
    assert p._max_tokens == 8192


def test_make_provider_gateway_falls_back_to_alias_when_cfg_model_empty(monkeypatch):
    cfg = {"base_url": "https://gw.example.com", "model": "", "auth_token": "t"}
    monkeypatch.setattr(gw, "gateway_api_config", lambda env=None: cfg)
    p = make_provider("opus", "api", gateway={"host": "gw"})
    assert p.model_id == CLAUDE_API_MODELS["opus"]
    assert p._base_url == "https://gw.example.com"


def test_make_provider_gateway_truthy_but_config_none_falls_through(monkeypatch):
    # Gateway lost between detection and call: build the plain API provider.
    monkeypatch.setattr(gw, "gateway_api_config", lambda env=None: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-plain")
    p = make_provider("opus", "api", gateway={"host": "gw"})
    assert isinstance(p, AnthropicProvider)
    assert p._base_url is None
    assert p._max_tokens == 4096  # server default, not the 8192 gateway path


def test_make_provider_server_path_gateway_none_is_byte_identical(monkeypatch):
    # The Docker server calls make_provider(model, 'api', gateway=None). The
    # gateway branch (and even the gateway_api_config import/call) must NOT run.
    calls = {"n": 0}

    def _boom(env=None):
        calls["n"] += 1
        raise AssertionError("gateway_api_config must not be called on the server path")

    monkeypatch.setattr(gw, "gateway_api_config", _boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-server")
    p = make_provider("opus", "api", gateway=None)
    assert isinstance(p, AnthropicProvider)
    assert p._base_url is None
    assert p._auth_token is None
    assert p._max_tokens == 4096
    assert calls["n"] == 0


def test_make_provider_api_default_gateway_arg_is_none(monkeypatch):
    monkeypatch.setattr(gw, "gateway_api_config", lambda env=None: (_ for _ in ()).throw(
        AssertionError("must not be called")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-default")
    p = make_provider("sonnet", "api")  # gateway omitted → defaults to None
    assert isinstance(p, AnthropicProvider)
    assert p._base_url is None


# ── Scenario 18: AnthropicProvider stores fields (construct only) ──────

def test_anthropic_provider_stores_gateway_fields():
    p = AnthropicProvider(
        "gw-model", "sk-fallback",
        base_url="http://gw.local/anthropic", auth_token="tok-xyz", max_tokens=8192,
    )
    assert p.model_id == "gw-model"
    assert p._model_name == "gw-model"
    assert p._api_key == "sk-fallback"
    assert p._base_url == "http://gw.local/anthropic"
    assert p._auth_token == "tok-xyz"
    assert p._max_tokens == 8192
    assert p.inline_files is True


def test_anthropic_provider_defaults_no_gateway():
    p = AnthropicProvider("claude-opus-4-6", "sk-1")
    assert p._base_url is None
    assert p._auth_token is None
    assert p._max_tokens == 4096


# ── Scenario 19: cc_switch_installed ───────────────────────────────────

def test_cc_switch_installed_true_when_dir_present(monkeypatch, tmp_path):
    d = tmp_path / ".cc-switch"
    d.mkdir()
    monkeypatch.setattr(gw, "CC_SWITCH_DIRS", (str(d),))
    assert cc_switch_installed() is True


def test_cc_switch_installed_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "CC_SWITCH_DIRS", (str(tmp_path / "nope"),))
    assert cc_switch_installed() is False


def test_cc_switch_installed_true_if_any_dir_present(monkeypatch, tmp_path):
    present = tmp_path / "app_support"
    present.mkdir()
    monkeypatch.setattr(gw, "CC_SWITCH_DIRS", (str(tmp_path / "missing"), str(present)))
    assert cc_switch_installed() is True


def test_cc_switch_installed_false_when_path_is_a_file(monkeypatch, tmp_path):
    f = tmp_path / "not_a_dir"
    f.write_text("x")
    monkeypatch.setattr(gw, "CC_SWITCH_DIRS", (str(f),))
    assert cc_switch_installed() is False  # isdir() is False for a plain file


# ── Helper-level coverage: _json_file_env / _manual_gateway_env, etc. ──

def test_json_file_env_env_block_overrides_top_level(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "ANTHROPIC_MODEL": "top-model",
        "ANTHROPIC_BASE_URL": "https://top.example.com",
        "env": {"ANTHROPIC_MODEL": "env-model"},
    }))
    out = _json_file_env(str(p))
    assert out["ANTHROPIC_MODEL"] == "env-model"        # env block wins
    assert out["ANTHROPIC_BASE_URL"] == "https://top.example.com"  # top-level kept


def test_json_file_env_missing_file(tmp_path):
    assert _json_file_env(str(tmp_path / "absent.json")) == {}


def test_json_file_env_non_object_top_level(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert _json_file_env(str(p)) == {}


def test_json_file_env_env_not_a_dict(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"env": "not-a-dict", "ANTHROPIC_BASE_URL": "https://x"}))
    out = _json_file_env(str(p))
    assert out == {"ANTHROPIC_BASE_URL": "https://x"}  # bad env ignored, top-level kept


def test_os_environ_env_returns_only_set_nonblank_keys(monkeypatch):
    for k in gw.GATEWAY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://x")
    monkeypatch.setenv("ANTHROPIC_MODEL", "  ")  # blank → dropped
    out = _os_environ_env()
    assert out == {"ANTHROPIC_BASE_URL": "https://x"}


def test_manual_gateway_env_maps_keys(tmp_path):
    p = tmp_path / "gateway.json"
    p.write_text(json.dumps({
        "base_url": "https://gw.local",
        "auth_token": "tok",
        "model": "m",
        "small_fast_model": "fast",
    }))
    out = _manual_gateway_env(str(p))
    assert out == {
        "ANTHROPIC_BASE_URL": "https://gw.local",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "ANTHROPIC_MODEL": "m",
        "ANTHROPIC_SMALL_FAST_MODEL": "fast",
    }


def test_manual_gateway_env_missing_returns_empty(tmp_path):
    assert _manual_gateway_env(str(tmp_path / "absent.json")) == {}


def test_has_gateway_predicate():
    assert _has_gateway({"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_AUTH_TOKEN": "t"}) is True
    assert _has_gateway({"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_API_KEY": "k"}) is True
    assert _has_gateway({"ANTHROPIC_BASE_URL": "https://x"}) is False
    assert _has_gateway({"ANTHROPIC_AUTH_TOKEN": "t"}) is False
    assert _has_gateway({}) is False


# ── Option-B write/read roundtrip and validation ───────────────────────

def test_write_manual_gateway_roundtrip_auth_token(tmp_path):
    p = tmp_path / "gateway.json"
    write_manual_gateway("https://gw.local/anthropic", auth_token="tok",
                         model="m", small_fast_model="fast", path=str(p))
    data = read_manual_gateway(str(p))
    assert data == {
        "base_url": "https://gw.local/anthropic",
        "auth_token": "tok",
        "model": "m",
        "small_fast_model": "fast",
    }


def test_write_manual_gateway_prefers_auth_token_over_api_key(tmp_path):
    p = tmp_path / "gateway.json"
    write_manual_gateway("https://gw.local", auth_token="tok", api_key="sk", path=str(p))
    data = read_manual_gateway(str(p))
    assert data["auth_token"] == "tok"
    assert "api_key" not in data


def test_write_manual_gateway_api_key_only(tmp_path):
    p = tmp_path / "gateway.json"
    write_manual_gateway("https://gw.local", api_key="sk-1", path=str(p))
    data = read_manual_gateway(str(p))
    assert data == {"base_url": "https://gw.local", "api_key": "sk-1"}


def test_write_manual_gateway_file_mode_is_0600(tmp_path):
    p = tmp_path / "gateway.json"
    write_manual_gateway("https://gw.local", auth_token="tok", path=str(p))
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_write_manual_gateway_requires_base_url(tmp_path):
    with pytest.raises(ValueError):
        write_manual_gateway("", auth_token="tok", path=str(tmp_path / "g.json"))


def test_write_manual_gateway_requires_credential(tmp_path):
    with pytest.raises(ValueError):
        write_manual_gateway("https://gw.local", path=str(tmp_path / "g.json"))


def test_read_manual_gateway_missing_returns_none(tmp_path):
    assert read_manual_gateway(str(tmp_path / "absent.json")) is None


def test_read_manual_gateway_malformed_returns_none(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("{ broken")
    assert read_manual_gateway(str(p)) is None


def test_read_manual_gateway_non_object_returns_none(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps([1, 2]))
    assert read_manual_gateway(str(p)) is None


# ── Injected env bypasses the source walk (verbatim) ───────────────────

def test_injected_env_bypasses_source_walk(sources):
    # A real source exists, but an explicit env arg must be used verbatim.
    _write_env_block(sources["claude_settings"], _env_with_model("fromfile"))
    gw_info = gateway_info(_env_with_model("injected"))
    assert gw_info["model"] == "injected"
    cfg = gateway_api_config(_env_with_model("injected"))
    assert cfg["model"] == "injected"


def test_gateway_api_config_end_to_end_from_source(sources):
    _write_env_block(sources["claude_settings"], GATEWAY_ENV)
    cfg = gateway_api_config()
    assert cfg == {
        "base_url": "https://gateway.example.com/anthropic",
        "model": "company-model",
        "auth_token": "tok-123",
    }


# ── Scenario 18: introspect_skill routes through the gateway on the api path ─
# Regression guard: in macOS gateway mode _skill_claude_path() returns "api",
# so introspect_skill must build its provider against the gateway (not the
# public API with an empty key). When no gateway is active (the Docker server)
# it must stay byte-identical to the plain public-API construction.

import figwatch.skills as skills  # noqa: E402


class _RecordingProvider:
    """Stands in for AnthropicProvider: records ctor kwargs, returns canned JSON."""

    inline_files = True
    last = None

    def __init__(self, model_name, api_key, rate_limiter=None, *,
                 base_url=None, auth_token=None, max_tokens=4096):
        self.model_id = model_name
        _RecordingProvider.last = {
            "model_name": model_name, "api_key": api_key,
            "base_url": base_url, "auth_token": auth_token,
        }

    def call(self, prompt, image_path):
        return ('{"comment_compatible": false, "incompatible_reason": "x", '
                '"required_data": ["screenshot"]}')


@pytest.fixture
def introspect_env(monkeypatch, tmp_path):
    """Hermetic introspect_skill: fake provider, no cache I/O, a real skill file."""
    _RecordingProvider.last = None
    monkeypatch.setattr(skills, "AnthropicProvider", _RecordingProvider)
    monkeypatch.setattr(skills, "_load_skill_cache", lambda: {})
    monkeypatch.setattr(skills, "_save_skill_cache", lambda cache: None)
    skill_file = tmp_path / "skill.md"
    skill_file.write_text("# some skill\nDo a thing.\n")
    return skill_file


def test_introspect_skill_api_uses_gateway_when_active(introspect_env, monkeypatch):
    monkeypatch.setattr(skills, "gateway_api_config", lambda: {
        "base_url": "http://llm-gw.jd.local/anthropic",
        "model": "Claude-Opus-4.8-joybuilder",
        "auth_token": "gw-tok",
    })
    result = skills.introspect_skill(str(introspect_env), "api")
    rec = _RecordingProvider.last
    assert rec["base_url"] == "http://llm-gw.jd.local/anthropic"
    assert rec["auth_token"] == "gw-tok"
    assert rec["model_name"] == "Claude-Opus-4.8-joybuilder"
    # The gateway provider's response is parsed and returned (not safe_default).
    assert result["comment_compatible"] is False


def test_introspect_skill_api_stays_plain_without_gateway(introspect_env, monkeypatch):
    monkeypatch.setattr(skills, "gateway_api_config", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-plain")
    skills.introspect_skill(str(introspect_env), "api")
    rec = _RecordingProvider.last
    assert rec["base_url"] is None
    assert rec["auth_token"] is None
    assert rec["model_name"] == CLAUDE_API_MODELS["haiku"]
    assert rec["api_key"] == "sk-plain"
