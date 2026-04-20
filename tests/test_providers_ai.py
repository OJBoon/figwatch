"""Tests for figwatch.providers.ai — provider routing and retry utilities."""

import pytest

from figwatch.providers.ai import (
    AIProvider,
    CLAUDE_API_MODELS,
    GEMINI_MODELS,
    make_provider,
    parse_retry_seconds,
    reset_limiters,
)
from figwatch.providers.ai.anthropic import AnthropicProvider
from figwatch.providers.ai.claude_cli import ClaudeCLIProvider
from figwatch.providers.ai.gemini import GeminiProvider
from figwatch.providers.ai.rate_limit import TokenBucket


@pytest.fixture(autouse=True)
def _reset_limiters():
    reset_limiters()
    yield
    reset_limiters()


# ── make_provider routing ─────────────────────────────────────────────

def test_make_provider_gemini_flash():
    p = make_provider("gemini-flash", "claude")
    assert isinstance(p, GeminiProvider)


def test_make_provider_gemini_alias():
    p = make_provider("gemini", "claude")
    assert isinstance(p, GeminiProvider)


def test_make_provider_gemini_full_model_id():
    p = make_provider("gemini-3.1-flash-lite-preview", "claude")
    assert isinstance(p, GeminiProvider)


def test_make_provider_anthropic_api():
    p = make_provider("sonnet", "api")
    assert isinstance(p, AnthropicProvider)


def test_make_provider_anthropic_all_aliases():
    for alias in ("sonnet", "opus", "haiku"):
        p = make_provider(alias, "api")
        assert isinstance(p, AnthropicProvider), f"Expected AnthropicProvider for {alias!r}"


def test_make_provider_claude_cli():
    p = make_provider("sonnet", "/usr/local/bin/claude")
    assert isinstance(p, ClaudeCLIProvider)


def test_make_provider_cli_passes_skill_dir():
    p = make_provider("sonnet", "/usr/local/bin/claude", skill_dir="/tmp/skills")
    assert isinstance(p, ClaudeCLIProvider)
    assert p._skill_dir == "/tmp/skills"


# ── Provider properties ───────────────────────────────────────────────

def test_gemini_provider_properties():
    p = GeminiProvider("gemini-flash", "key")
    assert p.model_id == "gemini-flash"
    assert p.inline_files is True


def test_anthropic_provider_properties():
    p = AnthropicProvider("claude-sonnet-4-6", "key")
    assert p.model_id == "claude-sonnet-4-6"
    assert p.inline_files is True


def test_claude_cli_provider_properties():
    p = ClaudeCLIProvider("sonnet", "claude")
    assert p.model_id == "sonnet"
    assert p.inline_files is False


# ── AIProvider Protocol ───────────────────────────────────────────────

def test_all_providers_satisfy_protocol():
    assert isinstance(GeminiProvider("m", "k"), AIProvider)
    assert isinstance(AnthropicProvider("m", "k"), AIProvider)
    assert isinstance(ClaudeCLIProvider("m", "p"), AIProvider)


# ── Model alias resolution ────────────────────────────────────────────

def test_claude_api_model_aliases():
    assert "sonnet" in CLAUDE_API_MODELS
    assert "opus" in CLAUDE_API_MODELS
    assert "haiku" in CLAUDE_API_MODELS
    assert all("claude-" in v for v in CLAUDE_API_MODELS.values())


def test_gemini_model_aliases():
    assert "gemini-flash" in GEMINI_MODELS
    assert "gemini-flash-lite" in GEMINI_MODELS


# ── parse_retry_seconds ───────────────────────────────────────────────

def test_parse_retry_seconds_retry_delay_format():
    assert parse_retry_seconds("retry_delay: 30 seconds") == 30


def test_parse_retry_seconds_retry_after_format():
    assert parse_retry_seconds("retry after 60") == 60


def test_parse_retry_seconds_no_hint_uses_default():
    assert parse_retry_seconds("something went wrong") == 5


def test_parse_retry_seconds_custom_default():
    assert parse_retry_seconds("no hint here", default=15) == 15


def test_parse_retry_seconds_case_insensitive():
    assert parse_retry_seconds("Retry After 45") == 45


# ── Rate limiter wiring ───────────────────────────────────────────────

def test_make_provider_wires_gemini_rate_limiter(monkeypatch):
    monkeypatch.setenv("FIGWATCH_GEMINI_RPM", "10")
    reset_limiters()
    p = make_provider("gemini-flash", "claude")
    assert isinstance(p._rate_limiter, TokenBucket)


def test_make_provider_wires_anthropic_rate_limiter(monkeypatch):
    monkeypatch.setenv("FIGWATCH_ANTHROPIC_RPM", "3")
    reset_limiters()
    p = make_provider("sonnet", "api")
    assert isinstance(p._rate_limiter, TokenBucket)


def test_make_provider_disables_gemini_limiter_when_zero(monkeypatch):
    monkeypatch.setenv("FIGWATCH_GEMINI_RPM", "0")
    reset_limiters()
    p = make_provider("gemini-flash", "claude")
    assert p._rate_limiter is None


def test_make_provider_disables_anthropic_limiter_when_zero(monkeypatch):
    monkeypatch.setenv("FIGWATCH_ANTHROPIC_RPM", "0")
    reset_limiters()
    p = make_provider("sonnet", "api")
    assert p._rate_limiter is None


def test_shared_limiter_across_provider_instances(monkeypatch):
    monkeypatch.setenv("FIGWATCH_GEMINI_RPM", "10")
    reset_limiters()
    p1 = make_provider("gemini-flash", "claude")
    p2 = make_provider("gemini-flash", "claude")
    assert p1._rate_limiter is p2._rate_limiter
