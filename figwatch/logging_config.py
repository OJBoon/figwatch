"""Logging configuration for FigWatch.

Text format (default) is tuned for Dozzle — inline key=value context fields
so the user can substring-search by audit_id, node_id, trigger, etc. and
see the entire pipeline for a single audit on one filtered view.

JSON format is opt-in for future log aggregator use.
"""

import json
import logging
import os
import sys
from typing import Iterable, Optional

from figwatch.log_context import get_audit_context

# Fields shown in text-mode output, in order. Anything in the audit context
# that isn't listed here is still included in JSON mode but not text mode.
_TEXT_CONTEXT_KEYS = ('audit', 'trigger', 'node', 'file', 'attempt')

# ANSI color codes for text mode (when stdout is a TTY).
_COLORS = {
    'DEBUG':    '\033[90m',   # grey
    'INFO':     '\033[36m',   # cyan
    'WARNING':  '\033[33m',   # yellow
    'ERROR':    '\033[31m',   # red
    'CRITICAL': '\033[35m',   # magenta
}
_RESET = '\033[0m'

# Short logger names for text mode — stripped figwatch. prefix and trimmed
# to keep the column narrow. e.g. figwatch.providers.ai.gemini → ai.gemini
_LOGGER_NAME_WIDTH = 12


class ContextFilter(logging.Filter):
    """Attach fields from the current audit context onto every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_audit_context()
        # Store full context for JSON mode
        record.audit_context = ctx
        # Also set individual attributes so existing extra={} semantics work
        for key, value in ctx.items():
            if not hasattr(record, key):
                setattr(record, key, value)

        # Inject OTel trace/span IDs when a span is active.
        record.trace_id = ''
        record.span_id = ''
        try:
            from opentelemetry import trace
            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                record.trace_id = format(ctx.trace_id, '032x')
                record.span_id = format(ctx.span_id, '016x')
        except ImportError:
            pass

        return True


def _short_logger_name(name: str) -> str:
    """figwatch.providers.ai.gemini → ai.gemini; figwatch.server → server."""
    if name.startswith('figwatch.'):
        name = name[len('figwatch.'):]
    parts = name.split('.')
    # Drop the 'providers' prefix since every provider is under it
    if parts and parts[0] == 'providers':
        parts = parts[1:]
    return '.'.join(parts) if parts else name


class TextFormatter(logging.Formatter):
    """Human-readable output with inline key=value context fields.

    Format:
        2026-04-14 19:19:06 INFO  server     audit=a3f9 trigger=@ux running skill
    """

    def __init__(self, use_color: bool = False):
        super().__init__()
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, '%Y-%m-%d %H:%M:%S')
        level = record.levelname
        name = _short_logger_name(record.name).ljust(_LOGGER_NAME_WIDTH)[:_LOGGER_NAME_WIDTH]

        # Build inline key=value context prefix
        ctx = getattr(record, 'audit_context', {}) or {}
        ctx_parts = []
        trace_id = getattr(record, 'trace_id', '')
        if trace_id:
            ctx_parts.append(f'trace={trace_id}')
        for key in _TEXT_CONTEXT_KEYS:
            if key in ctx:
                ctx_parts.append(f'{key}={ctx[key]}')
        ctx_str = (' '.join(ctx_parts) + ' ') if ctx_parts else ''

        # Any extra= fields attached directly to the record, excluding the
        # standard LogRecord attrs and our own context dict.
        extra_parts = []
        for key, value in record.__dict__.items():
            if key in _STD_RECORD_ATTRS or key in ('audit_context', 'trace_id', 'span_id'):
                continue
            if key in ctx:
                continue
            extra_parts.append(f'{key}={value}')
        extra_str = (' '.join(extra_parts) + ' ') if extra_parts else ''

        msg = record.getMessage()
        line = f'{ts} {level:<5} {name} {ctx_str}{extra_str}{msg}'

        if record.exc_info:
            line += '\n' + self.formatException(record.exc_info)

        if self._use_color:
            color = _COLORS.get(level, '')
            if color:
                line = f'{color}{line}{_RESET}'

        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line with flat context fields. Opt-in via env var."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
            'level': record.levelname,
            'logger': _short_logger_name(record.name),
            'msg': record.getMessage(),
        }

        ctx = getattr(record, 'audit_context', {}) or {}
        payload.update(ctx)

        trace_id = getattr(record, 'trace_id', '')
        span_id = getattr(record, 'span_id', '')
        if trace_id:
            payload['trace_id'] = trace_id
            payload['span_id'] = span_id

        for key, value in record.__dict__.items():
            if key in _STD_RECORD_ATTRS or key in ('audit_context', 'trace_id', 'span_id'):
                continue
            if key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


# Standard LogRecord attributes we should never re-emit as extras.
_STD_RECORD_ATTRS = frozenset({
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'taskName',
})


def configure_logging(
    level: Optional[str] = None,
    fmt: Optional[str] = None,
    *,
    stream=None,
) -> None:
    """Install a root logger configured for FigWatch.

    Reads FIGWATCH_LOG_LEVEL and FIGWATCH_LOG_FORMAT from env if arguments are
    None. Safe to call multiple times — replaces existing handlers on the
    root logger.
    """
    level_name = (level or os.environ.get('FIGWATCH_LOG_LEVEL') or 'INFO').upper()
    fmt_name = (fmt or os.environ.get('FIGWATCH_LOG_FORMAT') or 'text').lower()

    root = logging.getLogger()
    root.setLevel(level_name)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(ContextFilter())

    if fmt_name == 'json':
        handler.setFormatter(JsonFormatter())
    else:
        use_color = (stream or sys.stdout).isatty()
        handler.setFormatter(TextFormatter(use_color=use_color))

    root.addHandler(handler)

    # Silence noisy third-party loggers at INFO; promote them when user sets DEBUG.
    if level_name != 'DEBUG':
        for noisy in ('urllib3', 'httpx', 'httpcore'):
            logging.getLogger(noisy).setLevel(logging.WARNING)
