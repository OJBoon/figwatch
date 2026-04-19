"""Tests for figwatch.services — AuditService application layer."""

from unittest.mock import MagicMock, patch

import pytest

from figwatch.domain import (
    Audit, AuditCompleted, AuditFailed, AuditResult, AuditStatus,
    Comment, Trigger, TriggerMatch,
)
from figwatch.services import AuditConfig, AuditService


# ── Helpers ──────────────────────────────────────────────────────────

def _make_audit(audit_id="audit-1"):
    return Audit(
        audit_id=audit_id,
        comment=Comment(
            comment_id="c1", message="@ux check", parent_id="p1",
            node_id="2:3", user_handle="alice", file_key="file-1",
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(keyword="@ux", skill_ref="builtin:ux"),
            extra="check",
        ),
    )


def _make_config():
    return AuditConfig(model="gemini-flash", claude_path="api",
                       reply_lang="en", locale="uk")


def _make_service(comment_repo=None, design_repo=None, config=None):
    return AuditService(
        comment_repo=comment_repo or MagicMock(),
        design_repo=design_repo or MagicMock(),
        config=config or _make_config(),
        trigger_config=[{"trigger": "@ux", "skill": "builtin:ux"}],
    )


# ── Ack lifecycle ────────────────────────────────────────────────────

def test_post_ack_calls_repo():
    repo = MagicMock()
    repo.post_reply.return_value = "ack-42"
    service = _make_service(comment_repo=repo)
    audit = _make_audit()

    result = service.post_ack(audit, "hello")
    assert result == "ack-42"
    repo.post_reply.assert_called_once_with("file-1", "p1", "hello")


def test_delete_ack_calls_repo():
    repo = MagicMock()
    service = _make_service(comment_repo=repo)
    audit = _make_audit()

    service.delete_ack(audit, "ack-1")
    repo.delete_comment.assert_called_once_with("file-1", "ack-1")


def test_delete_ack_skips_none():
    repo = MagicMock()
    service = _make_service(comment_repo=repo)
    audit = _make_audit()

    service.delete_ack(audit, None)
    repo.delete_comment.assert_not_called()


def test_update_ack_deletes_then_posts():
    repo = MagicMock()
    repo.post_reply.return_value = "new-ack"
    service = _make_service(comment_repo=repo)
    audit = _make_audit()

    result = service.update_ack(audit, "old-ack", "new message")
    assert result == "new-ack"
    repo.delete_comment.assert_called_once()
    repo.post_reply.assert_called_once()


# ── Execute ──────────────────────────────────────────────────────────

@patch('figwatch.skills.execute_skill')
def test_execute_success(mock_skill):
    mock_skill.return_value = "Looks great!"
    service = _make_service()
    audit = _make_audit()

    result = service.execute(audit)
    assert "Looks great!" in result
    assert audit.status == AuditStatus.REPLIED
    events = audit.collect_events()
    assert any(isinstance(e, AuditCompleted) for e in events)


@patch('figwatch.skills.execute_skill')
def test_execute_failure(mock_skill):
    mock_skill.side_effect = RuntimeError("AI provider down")
    service = _make_service()
    audit = _make_audit()

    with pytest.raises(RuntimeError):
        service.execute(audit)
    assert audit.status == AuditStatus.ERROR
    events = audit.collect_events()
    assert any(isinstance(e, AuditFailed) for e in events)


@patch('figwatch.skills.execute_skill')
def test_execute_passes_config_and_repo(mock_skill):
    mock_skill.return_value = "ok"
    design_repo = MagicMock()
    config = _make_config()
    service = _make_service(design_repo=design_repo, config=config)
    audit = _make_audit()

    service.execute(audit)
    mock_skill.assert_called_once_with(
        audit, config=config, design_repo=design_repo,
    )


# ── Event dispatch ───────────────────────────────────────────────────

@patch('figwatch.metrics.record_audit_completed')
@patch('figwatch.skills.execute_skill')
def test_dispatch_events_success(mock_skill, mock_record):
    mock_skill.return_value = "ok"
    service = _make_service()
    audit = _make_audit()
    service.execute(audit)

    service.dispatch_events(audit, duration=5.0)
    mock_record.assert_called_once_with(5.0, 'success')


@patch('figwatch.metrics.record_audit_completed')
@patch('figwatch.skills.execute_skill')
def test_dispatch_events_failure(mock_skill, mock_record):
    mock_skill.side_effect = RuntimeError("boom")
    service = _make_service()
    audit = _make_audit()

    with pytest.raises(RuntimeError):
        service.execute(audit)

    service.dispatch_events(audit, duration=3.0)
    mock_record.assert_called_once_with(3.0, 'failed')


# ── Config ───────────────────────────────────────────────────────────

def test_audit_config_frozen():
    config = _make_config()
    with pytest.raises(AttributeError):
        config.model = "opus"


def test_service_exposes_config():
    config = _make_config()
    service = _make_service(config=config)
    assert service.config is config
