"""Tests for conversational @figwatch follow-up detection in detect_triggers."""

import figwatch.watcher as w
from figwatch.watcher import detect_triggers

TRIGGERS = [{"trigger": "@ux", "skill": "builtin:ux"}]

_SIGNOFF = "\n\n— FigWatch · ux skill · 3s · model"


def _c(cid, message, parent_id=None, node_id=None, handle="alice", created=1,
       resolved=False):
    d = {"id": cid, "message": message, "user": {"handle": handle},
         "created_at": f"2026-07-31T00:00:0{created}Z"}
    if parent_id:
        d["parent_id"] = parent_id
    if node_id:
        d["client_meta"] = {"node_id": node_id}
    if resolved:
        d["resolved_at"] = "2026-07-31T01:00:00Z"
    return d


def _run(monkeypatch, comments, processed=None, follow_up_mention="@figwatch"):
    monkeypatch.setattr(w, "figma_get", lambda *a, **k: {"comments": comments})
    monkeypatch.setattr(w, "save_processed", lambda *a, **k: None)
    processed = set() if processed is None else processed
    return detect_triggers("filekey", "pat", processed, TRIGGERS,
                           log=lambda *a, **k: None,
                           follow_up_mention=follow_up_mention)


def _audit_thread():
    return [
        _c("root", "@ux please review", node_id="1:2", handle="alice", created=1),
        _c("bot", "\U0001f5e3️ ux Audit — Home\n\nfindings here" + _SIGNOFF,
           parent_id="root", handle="figbot", created=2),
    ]


def test_followup_detected_and_routed(monkeypatch):
    comments = [
        *_audit_thread(),
        _c("fu1", "@figwatch focus on the CTA copy", parent_id="root",
           handle="alice", created=3),
    ]
    audits = _run(monkeypatch, comments)
    assert len(audits) == 1
    a = audits[0]
    assert a.is_refinement
    assert a.trigger_match.trigger.keyword == "@ux"
    assert a.trigger_match.trigger.skill_ref == "builtin:ux"
    assert a.trigger_match.extra == "focus on the CTA copy"   # mention stripped
    assert a.comment.node_id == "1:2"                          # inherited from root
    assert a.comment.parent_id == "root"
    # transcript = root (human) + bot audit, oldest first, bot chrome stripped
    assert len(a.thread_history) == 2
    assert a.thread_history[0].is_bot is False
    assert a.thread_history[1].is_bot is True
    assert "\U0001f5e3" not in a.thread_history[1].text     # header line stripped
    assert "FigWatch ·" not in a.thread_history[1].text  # sign-off stripped
    assert "findings here" in a.thread_history[1].text


def test_plain_reply_without_mention_ignored(monkeypatch):
    comments = [
        *_audit_thread(),
        _c("fu2", "hey what about the CTA", parent_id="root", created=3),
    ]
    audits = _run(monkeypatch, comments)
    assert audits == []


def test_mention_without_prior_audit_does_not_refine(monkeypatch):
    # A thread with a human trigger but no FigWatch audit yet: the @figwatch reply
    # must not refine. (The top-level @ux still audits normally.)
    comments = [
        _c("root", "@ux review", node_id="1:2", created=1),
        _c("fu", "@figwatch help", parent_id="root", created=2),
    ]
    audits = _run(monkeypatch, comments)
    assert all(not a.is_refinement for a in audits)


def test_bot_reply_never_treated_as_followup(monkeypatch):
    # Even if a bot reply contained the mention, its sign-off marker excludes it.
    comments = [
        *_audit_thread(),
        _c("bot2", "@figwatch note" + _SIGNOFF, parent_id="root",
           handle="figbot", created=3),
    ]
    audits = _run(monkeypatch, comments)
    assert audits == []


def test_followup_deduped_by_id(monkeypatch):
    comments = [
        *_audit_thread(),
        _c("fu1", "@figwatch more", parent_id="root", created=3),
    ]
    audits = _run(monkeypatch, comments, processed={"fu1"})
    assert audits == []


def test_disabled_when_mention_none(monkeypatch):
    comments = [
        *_audit_thread(),
        _c("fu1", "@figwatch focus on the CTA", parent_id="root", created=3),
    ]
    audits = _run(monkeypatch, comments, follow_up_mention=None)
    assert audits == []   # byte-identical to old behaviour


def test_transcript_capped(monkeypatch):
    big = "x" * 9000
    comments = [
        _c("root", "@ux review", node_id="1:2", created=1),
        _c("bot", "\U0001f5e3️ ux Audit\n\n" + big + _SIGNOFF,
           parent_id="root", handle="figbot", created=2),
        _c("fu1", "@figwatch and the header?", parent_id="root", created=3),
    ]
    audits = _run(monkeypatch, comments)
    assert len(audits) == 1
    hist = audits[0].thread_history
    # Oversize thread: oldest (root) dropped, the recent bot turn kept.
    assert len(hist) == 1
    assert hist[0].is_bot is True
