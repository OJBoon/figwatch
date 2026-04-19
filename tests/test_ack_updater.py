"""Tests for figwatch.ack_updater — AckUpdater background worker.

These tests use a FakeCommentRepository to avoid network calls.
"""

import threading
import time

import pytest

from figwatch.ack_updater import AckUpdater, PendingUpdate, _position_message
from figwatch.domain import Audit, Comment, Trigger, TriggerMatch
from figwatch.queue_stats import InstrumentedQueue, QueuedItem


# ── Helpers ───────────────────────────────────────────────────────────

def _make_audit(trigger='@ux', node_id='1:2', file_key='abc'):
    return Audit(
        audit_id='test',
        comment=Comment(
            comment_id='c1', message=f'{trigger} check', parent_id='111',
            node_id=node_id, user_handle='alice', file_key=file_key,
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(keyword=trigger, skill_ref='builtin:ux'),
            extra='',
        ),
    )


def _queued(audit_id, ack_id='ack-0'):
    return QueuedItem(
        audit=_make_audit(),
        ack_id=ack_id,
        audit_id=audit_id,
    )


class FakeCommentRepo:
    """Records post/delete calls and hands out predictable ack ids."""

    def __init__(self):
        self.posts = []
        self.deletes = []
        self._counter = 0

    def post_reply(self, file_key, parent_comment_id, message):
        self._counter += 1
        new_id = f'ack-{self._counter}'
        self.posts.append({'message': message, 'ack_id': new_id})
        return new_id

    def delete_comment(self, file_key, comment_id):
        self.deletes.append({'comment_id': comment_id})

    def fetch_comments(self, file_key):
        return []


@pytest.fixture
def repo():
    return FakeCommentRepo()


# ── _position_message formatting ─────────────────────────────────────

def test_position_message_zero():
    msg = _position_message('@ux', 0)
    assert 'starting shortly' in msg
    assert 'ux' in msg


def test_position_message_one():
    assert '1 ahead of you' in _position_message('@ux', 1)


def test_position_message_many():
    assert '5 ahead of you' in _position_message('@tone', 5)
    assert 'tone' in _position_message('@tone', 5)


def test_position_message_strips_trigger_prefix():
    assert '@ux' not in _position_message('@ux', 2)


# ── Public API surface ───────────────────────────────────────────────

def test_track_initial_records_position(repo):
    q = InstrumentedQueue()
    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('abc', position=3)
    assert updater._displayed['abc'] == 3


def test_cancel_removes_tracking(repo):
    q = InstrumentedQueue()
    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('abc', position=2)
    updater.cancel('abc')
    assert 'abc' not in updater._displayed
    assert 'abc' not in updater._pending


def test_rate_zero_disables_thread(repo):
    q = InstrumentedQueue()
    updater = AckUpdater(q, repo, rate_per_minute=0)
    updater.start()
    assert updater._thread is None
    updater.stop()


# ── _refresh_pending (position change detection) ─────────────────────

def test_refresh_skips_items_at_their_displayed_position(repo):
    q = InstrumentedQueue()
    q.put(_queued('a'))
    q.put(_queued('b'))
    q.put(_queued('c'))

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('a', position=0)
    updater.track_initial('b', position=1)
    updater.track_initial('c', position=2)

    updater._refresh_pending()
    assert updater._pending == {}


def test_refresh_schedules_update_when_position_moves(repo):
    q = InstrumentedQueue()
    q.put(_queued('a'))
    q.put(_queued('b'))
    q.put(_queued('c'))

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('a', position=0)
    updater.track_initial('b', position=1)
    updater.track_initial('c', position=2)

    # Remove 'a' — positions shift: b is now 0, c is now 1
    q.get(timeout=1)
    updater._refresh_pending()

    assert 'b' in updater._pending
    assert 'c' in updater._pending
    assert updater._pending['b'].new_position == 0
    assert updater._pending['c'].new_position == 1


def test_refresh_coalesces_multiple_updates_for_same_audit(repo):
    q = InstrumentedQueue()
    q.put(_queued('a'))
    q.put(_queued('b'))
    q.put(_queued('c'))
    q.put(_queued('d'))

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('a', position=0)
    updater.track_initial('b', position=1)
    updater.track_initial('c', position=2)
    updater.track_initial('d', position=3)

    updater._refresh_pending()
    assert updater._pending == {}

    q.get(timeout=1)
    updater._refresh_pending()
    assert updater._pending['d'].new_position == 2

    q.get(timeout=1)
    updater._refresh_pending()
    assert updater._pending['d'].new_position == 1


def test_refresh_cleans_up_stale_displayed_entries(repo):
    q = InstrumentedQueue()
    q.put(_queued('a'))

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('a', position=0)
    updater._displayed['ghost'] = 2
    updater._pending['ghost'] = PendingUpdate(
        audit_id='ghost', new_position=1, queued=_queued('ghost'),
    )

    updater._refresh_pending()
    assert 'ghost' not in updater._displayed
    assert 'ghost' not in updater._pending


# ── _post_one (actual ack update posting) ────────────────────────────

def test_post_one_posts_pending_update(repo):
    q = InstrumentedQueue()
    queued = _queued('a', ack_id='ack-0')
    q.put(queued)

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater._pending['a'] = PendingUpdate(
        audit_id='a', new_position=2, queued=queued,
    )

    updater._post_one()
    assert len(repo.posts) == 1
    assert len(repo.deletes) == 1
    assert repo.deletes[0]['comment_id'] == 'ack-0'
    assert '2 ahead' in repo.posts[0]['message']
    assert queued.ack_id == 'ack-1'
    assert updater._displayed['a'] == 2


def test_post_one_skips_if_audit_no_longer_queued(repo):
    q = InstrumentedQueue()
    queued = _queued('a')

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater._pending['a'] = PendingUpdate(
        audit_id='a', new_position=1, queued=queued,
    )

    updater._post_one()
    assert repo.posts == []
    assert repo.deletes == []


def test_post_one_noops_when_pending_empty(repo):
    q = InstrumentedQueue()
    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater._post_one()
    assert repo.posts == []


def test_post_one_respects_rate_limit(repo):
    q = InstrumentedQueue()
    for audit_id in ('a', 'b', 'c'):
        q.put(_queued(audit_id))

    updater = AckUpdater(q, repo, rate_per_minute=1, poll_seconds=0.01)
    updater._pending['a'] = PendingUpdate(
        audit_id='a', new_position=1, queued=q.find('a'),
    )
    updater._pending['b'] = PendingUpdate(
        audit_id='b', new_position=2, queued=q.find('b'),
    )

    updater._post_one()
    assert len(repo.posts) == 1

    updater._post_one()
    assert len(repo.posts) == 1  # throttled
    assert 'b' in updater._pending  # re-queued


# ── End-to-end: start / stop thread ──────────────────────────────────

def test_start_stop_thread_runs_cleanly(repo):
    q = InstrumentedQueue()
    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.start()
    assert updater._thread is not None
    assert updater._thread.is_alive()
    updater.stop()
    assert updater._thread is None


def test_thread_posts_updates_when_queue_moves(repo):
    q = InstrumentedQueue()
    q.put(_queued('a'))
    q.put(_queued('b'))
    q.put(_queued('c'))

    updater = AckUpdater(q, repo, rate_per_minute=60, poll_seconds=0.01)
    updater.track_initial('a', position=0)
    updater.track_initial('b', position=1)
    updater.track_initial('c', position=2)
    updater.start()

    try:
        q.get(timeout=1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if len(repo.posts) >= 2:
                break
            time.sleep(0.02)
    finally:
        updater.stop()

    assert len(repo.posts) >= 2
    positions = [p['message'] for p in repo.posts]
    assert any('starting shortly' in m for m in positions)
    assert any('1 ahead of you' in m for m in positions)
