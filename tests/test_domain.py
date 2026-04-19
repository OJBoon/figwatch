"""Tests for figwatch.domain — trigger matching, value objects, and Audit aggregate."""

import pytest

from figwatch.domain import (
    Audit,
    AuditCompleted,
    AuditFailed,
    AuditQueued,
    AuditResult,
    AuditStarted,
    AuditStatus,
    Comment,
    STATUS_DETECTED,
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_REPLIED,
    Trigger,
    TriggerMatch,
    WorkItem,
    match_trigger,
)


# ── match_trigger ─────────────────────────────────────────────────────

TRIGGERS = [
    {"trigger": "@ux", "skill": "builtin:ux"},
    {"trigger": "@tone", "skill": "builtin:tone"},
]


def test_match_trigger_exact():
    result = match_trigger("@ux", TRIGGERS)
    assert result.trigger.keyword == "@ux"
    assert result.trigger.skill_ref == "builtin:ux"
    assert result.extra == ""


def test_match_trigger_with_extra():
    result = match_trigger("@ux please check the nav", TRIGGERS)
    assert result.trigger.keyword == "@ux"
    assert result.extra == "please check the nav"


def test_match_trigger_mid_message():
    result = match_trigger("hey can you @tone this screen", TRIGGERS)
    assert result.trigger.keyword == "@tone"


def test_match_trigger_case_insensitive():
    result = match_trigger("@UX audit this", TRIGGERS)
    assert result is not None
    assert result.trigger.keyword == "@ux"


def test_match_trigger_no_match():
    assert match_trigger("just a regular comment", TRIGGERS) is None


def test_match_trigger_empty_message():
    assert match_trigger("", TRIGGERS) is None


def test_match_trigger_first_wins():
    result = match_trigger("@ux @tone", TRIGGERS)
    assert result.trigger.keyword == "@ux"


def test_match_trigger_returns_trigger_match():
    result = match_trigger("@ux check this", TRIGGERS)
    assert isinstance(result, TriggerMatch)
    assert isinstance(result.trigger, Trigger)


# ── Deprecated STATUS_* constants ────────────────────────────────────

def test_status_constants_match_enum():
    assert STATUS_DETECTED == AuditStatus.DETECTED.value
    assert STATUS_PROCESSING == AuditStatus.PROCESSING.value
    assert STATUS_REPLIED == AuditStatus.REPLIED.value
    assert STATUS_ERROR == AuditStatus.ERROR.value


# ── Value objects (frozen) ───────────────────────────────────────────

def test_trigger_frozen():
    t = Trigger(keyword="@ux", skill_ref="builtin:ux")
    with pytest.raises(AttributeError):
        t.keyword = "@tone"


def test_trigger_match_frozen():
    tm = TriggerMatch(trigger=Trigger(keyword="@ux", skill_ref="builtin:ux"), extra="")
    with pytest.raises(AttributeError):
        tm.extra = "new"


def test_comment_frozen():
    c = Comment(comment_id="1", message="hi", parent_id=None,
                node_id="2:3", user_handle="alice", file_key="abc")
    with pytest.raises(AttributeError):
        c.message = "bye"


def test_audit_result_frozen():
    r = AuditResult(reply_text="ok", provider_name="Claude", frame_name="Frame 1")
    with pytest.raises(AttributeError):
        r.reply_text = "no"


# ── Audit aggregate ─────────────────────────────────────────────────

def _make_audit(audit_id="audit-1"):
    return Audit(
        audit_id=audit_id,
        comment=Comment(
            comment_id="c1", message="@ux check", parent_id="p1",
            node_id="2:3", user_handle="alice", file_key="abc",
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(keyword="@ux", skill_ref="builtin:ux"),
            extra="check",
        ),
    )


def test_audit_initial_status():
    audit = _make_audit()
    assert audit.status == AuditStatus.DETECTED


def test_audit_reply_to_id_with_parent():
    audit = _make_audit()
    assert audit.reply_to_id == "p1"


def test_audit_reply_to_id_without_parent():
    audit = Audit(
        audit_id="a1",
        comment=Comment(
            comment_id="c1", message="@ux", parent_id=None,
            node_id="2:3", user_handle="alice", file_key="abc",
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(keyword="@ux", skill_ref="builtin:ux"), extra="",
        ),
    )
    assert audit.reply_to_id == "c1"


def test_audit_queue_transition():
    audit = _make_audit()
    audit.queue()
    assert audit.status == AuditStatus.QUEUED
    events = audit.collect_events()
    assert len(events) == 1
    assert isinstance(events[0], AuditQueued)
    assert events[0].audit_id == "audit-1"


def test_audit_start_processing():
    audit = _make_audit()
    audit.start_processing()
    assert audit.status == AuditStatus.PROCESSING
    events = audit.collect_events()
    assert isinstance(events[0], AuditStarted)


def test_audit_complete():
    audit = _make_audit()
    result = AuditResult(reply_text="looks good", provider_name="Claude", frame_name="Frame 1")
    audit.complete(result)
    assert audit.status == AuditStatus.REPLIED
    events = audit.collect_events()
    assert isinstance(events[0], AuditCompleted)
    assert events[0].result == result


def test_audit_fail():
    audit = _make_audit()
    audit.fail("timeout")
    assert audit.status == AuditStatus.ERROR
    events = audit.collect_events()
    assert isinstance(events[0], AuditFailed)
    assert events[0].error == "timeout"


def test_audit_collect_events_clears():
    audit = _make_audit()
    audit.queue()
    audit.start_processing()
    events = audit.collect_events()
    assert len(events) == 2
    assert audit.collect_events() == []


# ── WorkItem (deprecated) ───────────────────────────────────────────

def test_work_item_is_namedtuple():
    item = WorkItem(
        file_key="abc", comment_id="1", reply_to_id="1", node_id="2:3",
        trigger="@ux", skill_path="builtin:ux", user_handle="alice", extra="",
        locale="uk", model="gemini-flash", reply_lang="en", pat="figd_x",
        claude_path="api", on_status=None,
    )
    assert item.file_key == "abc"
    assert item.trigger == "@ux"


def test_work_item_replace():
    item = WorkItem(
        file_key="abc", comment_id="1", reply_to_id="1", node_id="2:3",
        trigger="@ux", skill_path="builtin:ux", user_handle="alice", extra="",
        locale=None, model=None, reply_lang=None, pat="figd_x",
        claude_path=None, on_status=None,
    )
    filled = item._replace(locale="uk", model="gemini-flash", reply_lang="en", claude_path="api")
    assert filled.locale == "uk"
    assert filled.model == "gemini-flash"
