"""Tests for figwatch.log_context and ContextFilter → LogRecord propagation."""

import json
import logging
import io

import pytest

from figwatch.log_context import (
    clear_audit_context,
    get_audit_context,
    new_audit_id,
    reset_audit_context,
    set_audit_context,
)
from figwatch.logging_config import (
    ContextFilter,
    JsonFormatter,
    TextFormatter,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _clear_context():
    clear_audit_context()
    yield
    clear_audit_context()


# ── log_context ──────────────────────────────────────────────────────

def test_new_audit_id_is_8_chars():
    assert len(new_audit_id()) == 8


def test_new_audit_id_is_unique():
    assert new_audit_id() != new_audit_id()


def test_set_and_get_context():
    set_audit_context(audit='abc', trigger='@ux')
    ctx = get_audit_context()
    assert ctx['audit'] == 'abc'
    assert ctx['trigger'] == '@ux'


def test_set_context_merges_existing_fields():
    set_audit_context(audit='abc')
    set_audit_context(trigger='@ux')
    ctx = get_audit_context()
    assert ctx == {'audit': 'abc', 'trigger': '@ux'}


def test_reset_context_restores_prior_state():
    set_audit_context(audit='first')
    token = set_audit_context(audit='second', trigger='@ux')
    reset_audit_context(token)
    ctx = get_audit_context()
    assert ctx == {'audit': 'first'}


def test_clear_context_empties_regardless_of_state():
    set_audit_context(audit='abc', trigger='@ux')
    clear_audit_context()
    assert get_audit_context() == {}


# ── ContextFilter attaches context to LogRecord ──────────────────────

def test_context_filter_attaches_fields_to_log_record(caplog):
    set_audit_context(audit='abc', trigger='@ux', node='1:2')
    logger = logging.getLogger('figwatch.test.ctx')
    # caplog auto-attaches a handler; add our filter to it
    for handler in logging.getLogger().handlers + [caplog.handler]:
        handler.addFilter(ContextFilter())

    with caplog.at_level(logging.DEBUG):
        logger.info('hello')

    record = caplog.records[-1]
    assert getattr(record, 'audit', None) == 'abc'
    assert getattr(record, 'trigger', None) == '@ux'
    assert getattr(record, 'node', None) == '1:2'
    assert record.audit_context == {'audit': 'abc', 'trigger': '@ux', 'node': '1:2'}


def test_context_filter_empty_when_no_context_set(caplog):
    logger = logging.getLogger('figwatch.test.ctx.empty')
    for handler in logging.getLogger().handlers + [caplog.handler]:
        handler.addFilter(ContextFilter())
    with caplog.at_level(logging.DEBUG):
        logger.info('hello')
    record = caplog.records[-1]
    assert record.audit_context == {}


# ── TextFormatter output ─────────────────────────────────────────────

def _format_record(formatter, name, level, msg, **ctx):
    logger = logging.getLogger(name)
    record = logger.makeRecord(
        name=name,
        level=level,
        fn='',
        lno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    record.audit_context = ctx
    for key, value in ctx.items():
        setattr(record, key, value)
    return formatter.format(record)


def test_text_formatter_includes_context_fields():
    fmt = TextFormatter(use_color=False)
    out = _format_record(
        fmt, 'figwatch.server', logging.INFO,
        'webhook received',
        audit='a3f9', trigger='@ux', node='1:2',
    )
    assert 'audit=a3f9' in out
    assert 'trigger=@ux' in out
    assert 'node=1:2' in out
    assert 'webhook received' in out
    assert 'INFO' in out


def test_text_formatter_shortens_logger_name():
    fmt = TextFormatter(use_color=False)
    out = _format_record(fmt, 'figwatch.providers.ai.gemini', logging.INFO, 'ping')
    # figwatch. prefix stripped, providers. prefix stripped
    assert 'ai.gemini' in out
    assert 'figwatch' not in out


def test_text_formatter_no_color_when_disabled():
    fmt = TextFormatter(use_color=False)
    out = _format_record(fmt, 'figwatch.server', logging.ERROR, 'boom')
    assert '\033[' not in out


def test_text_formatter_color_when_enabled():
    fmt = TextFormatter(use_color=True)
    out = _format_record(fmt, 'figwatch.server', logging.ERROR, 'boom')
    assert '\033[31m' in out
    assert '\033[0m' in out


# ── JsonFormatter output ─────────────────────────────────────────────

def test_json_formatter_produces_valid_json():
    fmt = JsonFormatter()
    out = _format_record(
        fmt, 'figwatch.server', logging.INFO,
        'webhook received',
        audit='a3f9', trigger='@ux',
    )
    payload = json.loads(out)
    assert payload['level'] == 'INFO'
    assert payload['logger'] == 'server'
    assert payload['msg'] == 'webhook received'
    assert payload['audit'] == 'a3f9'
    assert payload['trigger'] == '@ux'


def test_json_formatter_flattens_context():
    fmt = JsonFormatter()
    out = _format_record(
        fmt, 'figwatch.skills', logging.DEBUG,
        'running skill', audit='a3f9',
    )
    payload = json.loads(out)
    # 'audit_context' nested dict should not appear in JSON — context is flat
    assert 'audit_context' not in payload
    assert payload['audit'] == 'a3f9'


# ── configure_logging ────────────────────────────────────────────────

def test_configure_logging_text_mode_by_default():
    buf = io.StringIO()
    configure_logging(level='INFO', fmt='text', stream=buf)
    logger = logging.getLogger('figwatch.test.config')
    set_audit_context(audit='zzz')
    logger.info('hello there')
    out = buf.getvalue()
    assert 'audit=zzz' in out
    assert 'hello there' in out
    assert not out.strip().startswith('{')  # not JSON


def test_configure_logging_json_mode():
    buf = io.StringIO()
    configure_logging(level='INFO', fmt='json', stream=buf)
    logger = logging.getLogger('figwatch.test.config.json')
    set_audit_context(audit='zzz')
    logger.info('hello there')
    line = buf.getvalue().strip()
    payload = json.loads(line)
    assert payload['msg'] == 'hello there'
    assert payload['audit'] == 'zzz'


def test_configure_logging_respects_level():
    buf = io.StringIO()
    configure_logging(level='WARNING', fmt='text', stream=buf)
    logger = logging.getLogger('figwatch.test.level')
    logger.info('should be hidden')
    logger.warning('should be visible')
    out = buf.getvalue()
    assert 'should be hidden' not in out
    assert 'should be visible' in out
