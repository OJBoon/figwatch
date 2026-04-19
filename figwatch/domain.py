"""FigWatch domain types — pure module, no I/O.

Defines the Audit aggregate root, value objects, domain events,
and the AuditStatus enum for the Comment Auditing bounded context.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Union


# ── Audit status ─────────────────────────────────────────────────────

class AuditStatus(Enum):
    DETECTED = 'detected'
    QUEUED = 'queued'
    PROCESSING = 'processing'
    REPLIED = 'replied'
    ERROR = 'error'


STATUS_LIVE = 'live'


# ── Value objects (frozen) ───────────────────────────────────────────

@dataclass(frozen=True)
class Trigger:
    keyword: str       # e.g. "@ux"
    skill_ref: str     # e.g. "builtin:ux" or "/path/to/skill.md"


@dataclass(frozen=True)
class TriggerMatch:
    trigger: Trigger
    extra: str         # additional context after the trigger word


@dataclass(frozen=True)
class Comment:
    comment_id: str
    message: str
    parent_id: Optional[str]
    node_id: Optional[str]
    user_handle: str
    file_key: str


@dataclass(frozen=True)
class AuditResult:
    reply_text: str


# ── Domain events (frozen) ──────────────────────────────────────────

@dataclass(frozen=True)
class TriggerDetected:
    audit_id: str
    trigger_keyword: str
    file_key: str
    node_id: str


@dataclass(frozen=True)
class AuditQueued:
    audit_id: str


@dataclass(frozen=True)
class AuditStarted:
    audit_id: str


@dataclass(frozen=True)
class AuditCompleted:
    audit_id: str
    result: AuditResult


@dataclass(frozen=True)
class AuditFailed:
    audit_id: str
    error: str


DomainEvent = Union[TriggerDetected, AuditQueued, AuditStarted, AuditCompleted, AuditFailed]


# ── Audit aggregate root ────────────────────────────────────────────

@dataclass
class Audit:
    """Aggregate root — one per trigger comment detected."""
    audit_id: str
    comment: Comment
    trigger_match: TriggerMatch
    status: AuditStatus = AuditStatus.DETECTED
    _events: List[DomainEvent] = field(default_factory=list, repr=False)

    @property
    def reply_to_id(self) -> str:
        return self.comment.parent_id or self.comment.comment_id

    def queue(self):
        self.status = AuditStatus.QUEUED
        self._events.append(AuditQueued(self.audit_id))

    def start_processing(self):
        self.status = AuditStatus.PROCESSING
        self._events.append(AuditStarted(self.audit_id))

    def complete(self, result: AuditResult):
        self.status = AuditStatus.REPLIED
        self._events.append(AuditCompleted(self.audit_id, result))

    def fail(self, error: str):
        self.status = AuditStatus.ERROR
        self._events.append(AuditFailed(self.audit_id, error))

    def collect_events(self) -> list:
        events, self._events = self._events, []
        return events


# ── Pure domain functions ────────────────────────────────────────────

def match_trigger(message, trigger_config):
    """Match a comment message against configured triggers.

    Returns a TriggerMatch or None.
    """
    lower = message.lower().strip()
    for entry in trigger_config:
        trigger = entry.get('trigger', '')
        if trigger and trigger.lower() in lower:
            idx = lower.index(trigger.lower())
            extra = message[idx + len(trigger):].strip()
            return TriggerMatch(
                trigger=Trigger(keyword=trigger, skill_ref=entry.get('skill', '')),
                extra=extra,
            )
    return None
