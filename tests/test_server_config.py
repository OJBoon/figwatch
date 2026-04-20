"""Tests for fail-fast config validation in server.py:main() (ADR-001)."""

import logging
from unittest import mock

import pytest


# Minimal valid env — enough for main() to get past required-var checks.
_VALID_ENV = {
    'FIGMA_PAT': 'test-pat',
    'FIGWATCH_WEBHOOK_PASSCODE': 'test-passcode',
}


def _env(**overrides):
    """Return a complete env dict with overrides applied."""
    env = {**_VALID_ENV, **overrides}
    return env


def _run_main(env):
    """Import and call server.main() under a patched environment.

    Patches everything after validation so we never bind a port or start threads.
    """
    with mock.patch.dict('os.environ', env, clear=True), \
         mock.patch('server.configure_logging'), \
         mock.patch('server.init_metrics'), \
         mock.patch('server.validate_token', return_value='testuser'), \
         mock.patch('server.load_trigger_config', return_value=[]), \
         mock.patch('server.load_processed', return_value=set()), \
         mock.patch('server.AckUpdater'), \
         mock.patch('server.HTTPServer'), \
         mock.patch('server.threading.Thread'), \
         mock.patch('signal.signal'):
        import server
        server.main()


# ── FIGWATCH_MODEL ────────────────────────────────────────────────────


def test_invalid_model_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_MODEL='gpt-4'))


def test_valid_model_accepted():
    _run_main(_env(FIGWATCH_MODEL='gemini-flash'))


# ── FIGWATCH_LOCALE ───────────────────────────────────────────────────


def test_invalid_locale_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_LOCALE='us'))


def test_valid_locale_accepted():
    _run_main(_env(FIGWATCH_LOCALE='de'))


# ── FIGWATCH_MAX_ATTEMPTS ────────────────────────────────────────────


def test_max_attempts_zero_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_MAX_ATTEMPTS='0'))


def test_max_attempts_negative_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_MAX_ATTEMPTS='-1'))


def test_max_attempts_valid():
    _run_main(_env(FIGWATCH_MAX_ATTEMPTS='5'))


# ── FIGWATCH_WORKERS ─────────────────────────────────────────────────


def test_workers_zero_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_WORKERS='0'))


def test_workers_valid():
    _run_main(_env(FIGWATCH_WORKERS='2'))


# ── FIGWATCH_PORT ────────────────────────────────────────────────────


def test_port_zero_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_PORT='0'))


def test_port_too_high_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_PORT='70000'))


def test_port_valid():
    _run_main(_env(FIGWATCH_PORT='3000'))


# ── FIGWATCH_QUEUE_UPDATE_RPM ────────────────────────────────────────


def test_queue_update_rpm_zero_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_QUEUE_UPDATE_RPM='0'))


def test_queue_update_rpm_valid():
    _run_main(_env(FIGWATCH_QUEUE_UPDATE_RPM='10'))


# ── FIGWATCH_GEMINI_RPM / FIGWATCH_ANTHROPIC_RPM ────────────────────


def test_gemini_rpm_negative_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_GEMINI_RPM='-1'))


def test_gemini_rpm_zero_accepted():
    """RPM=0 disables rate limiting — valid."""
    _run_main(_env(FIGWATCH_GEMINI_RPM='0'))


def test_anthropic_rpm_negative_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_ANTHROPIC_RPM='-1'))


def test_anthropic_rpm_zero_accepted():
    """RPM=0 disables rate limiting — valid."""
    _run_main(_env(FIGWATCH_ANTHROPIC_RPM='0'))


# ── FIGWATCH_FIGMA_PLAN / FIGWATCH_FIGMA_SEAT ──────────────────────


def test_figma_plan_invalid_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_FIGMA_PLAN='invalid'))


def test_figma_seat_invalid_exits():
    with pytest.raises(SystemExit):
        _run_main(_env(FIGWATCH_FIGMA_SEAT='admin'))


def test_figma_plan_valid():
    _run_main(_env(FIGWATCH_FIGMA_PLAN='organization', FIGWATCH_FIGMA_SEAT='dev'))


# ── FIGMA_PAT token validation at startup ───────────────────────────


def test_expired_token_exits():
    from figwatch.providers.figma import FigmaTokenExpired

    with mock.patch.dict('os.environ', _VALID_ENV, clear=True), \
         mock.patch('server.configure_logging'), \
         mock.patch('server.validate_token', side_effect=FigmaTokenExpired('expired')):
        import server
        with pytest.raises(SystemExit):
            server.main()


def test_invalid_token_exits():
    with mock.patch.dict('os.environ', _VALID_ENV, clear=True), \
         mock.patch('server.configure_logging'), \
         mock.patch('server.validate_token', side_effect=RuntimeError('bad token')):
        import server
        with pytest.raises(SystemExit):
            server.main()


def test_skip_token_check():
    _run_main(_env(FIGWATCH_SKIP_TOKEN_CHECK='1'))
