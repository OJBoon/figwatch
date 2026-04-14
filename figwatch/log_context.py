"""Per-audit log context propagation via contextvars.

The webhook handler and worker loop set context fields (audit_id, trigger,
node_id, etc.) on entry, and the logging ContextFilter automatically attaches
those fields to every LogRecord produced within that context — so downstream
code (processor, skills, providers) can just call logger.info(...) and get
the audit correlation for free.
"""

import contextvars
import uuid
from typing import Any, Dict

AuditContext = Dict[str, Any]

_audit_ctx: contextvars.ContextVar[AuditContext] = contextvars.ContextVar(
    'audit_ctx', default={},
)


def new_audit_id() -> str:
    """Generate a short (8-char) audit ID suitable for visual correlation."""
    return uuid.uuid4().hex[:8]


def set_audit_context(**fields: Any) -> contextvars.Token:
    """Merge fields into the current audit context and return a reset token.

    Pass the returned token to reset_audit_context() when the scope exits.
    """
    current = dict(_audit_ctx.get())
    current.update(fields)
    return _audit_ctx.set(current)


def get_audit_context() -> AuditContext:
    """Return the current audit context (read-only snapshot)."""
    return dict(_audit_ctx.get())


def reset_audit_context(token: contextvars.Token) -> None:
    """Reset context to the state before the matching set_audit_context call."""
    _audit_ctx.reset(token)


def clear_audit_context() -> None:
    """Reset context to an empty dict regardless of prior state."""
    _audit_ctx.set({})
