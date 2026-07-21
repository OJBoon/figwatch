"""Tests for figwatch.ack_updater — AckUpdater background worker.

Uses FakeCommentRepo and FakeQueueRepo to avoid network/DB calls.
"""

import time

import pytest

from figwatch.ack_updater import AckUpdater, _position_message
from figwatch.domain import Audit, Comment, Trigger, TriggerMatch
from figwatch.ports import AckUpdateRow

# ── Helpers ───────────────────────────────────────────────────────────


def _make_audit(audit_id='test', trigger='@ux', node_id='1:2', file_key='abc'):
    return Audit(
        audit_id=audit_id,
        comment=Comment(
            comment_id='c1', message=f'{trigger} check', parent_id='111',
            node_id=node_id, user_handle='alice', file_key=file_key,
        ),
        trigger_match=TriggerMatch(
            trigger=Trigger(keyword=trigger, skill_ref='builtin:ux'),
            extra='',
        ),
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

    def comment_exists(self, file_key, comment_id):
        return True

    def fetch_comments(self, file_key):
        return []


class FakeQueueRepo:
    """In-memory AuditQueueRepository for testing AckUpdater."""

    def __init__(self):
        self._items = []  # list of (audit_id, audit, ack_id, ack_position, enqueued_at)
        self._ack_updates = []  # record of update_ack calls

    def add_item(self, audit_id, ack_id='ack-0', ack_position=None):
        audit = _make_audit(audit_id=audit_id)
        self._items.append({
            'audit_id': audit_id,
            'audit': audit,
            'ack_id': ack_id,
            'ack_position': ack_position,
        })

    def remove_item(self, audit_id):
        self._items = [i for i in self._items if i['audit_id'] != audit_id]

    def pending_ack_updates(self):
        result = []
        for idx, item in enumerate(self._items):
            current_position = idx
            displayed = item['ack_position']
            if displayed is not None and current_position == displayed:
                continue
            result.append(AckUpdateRow(
                audit_id=item['audit_id'],
                audit=item['audit'],
                ack_id=item['ack_id'],
                current_position=current_position,
                displayed_position=displayed,
            ))
        return result

    def update_ack(self, audit_id, ack_id, position):
        self._ack_updates.append({
            'audit_id': audit_id, 'ack_id': ack_id, 'position': position,
        })
        for item in self._items:
            if item['audit_id'] == audit_id:
                item['ack_id'] = ack_id
                item['ack_position'] = position
                break

    def queue_depth(self):
        return len(self._items)


@pytest.fixture
def comment_repo():
    return FakeCommentRepo()


@pytest.fixture
def queue_repo():
    return FakeQueueRepo()


# ── _position_message formatting ─────────────────────────────────────

def test_position_message_zero():
    msg = _position_message('@ux', 0, '\ntrace id: abc12345')
    assert 'starting shortly' in msg
    assert 'ux' in msg
    assert 'trace id: abc12345' in msg


def test_position_message_one():
    assert '1 ahead of you' in _position_message('@ux', 1, '\ntrace id: abc12345')


def test_position_message_many():
    assert '5 ahead of you' in _position_message('@tone', 5, '\ntrace id: abc12345')
    assert 'tone' in _position_message('@tone', 5, '\ntrace id: abc12345')


def test_position_message_strips_trigger_prefix():
    assert '@ux' not in _position_message('@ux', 2, '\ntrace id: abc12345')


def test_position_message_empty_trace():
    msg = _position_message('@ux', 0, '')
    assert 'trace id' not in msg
    assert 'starting shortly' in msg


# ── AckUpdater behavior ─────────────────────────────────────────────

def test_rate_zero_disables_thread(comment_repo, queue_repo):
    updater = AckUpdater(queue_repo, comment_repo, rate_per_minute=0)
    updater.start()
    assert updater._thread is None
    updater.stop()


def test_no_updates_when_positions_unchanged(comment_repo, queue_repo):
    queue_repo.add_item('a', ack_position=0)
    queue_repo.add_item('b', ack_position=1)
    queue_repo.add_item('c', ack_position=2)

    AckUpdater(queue_repo, comment_repo, rate_per_minute=60, poll_seconds=0.01)
    # Manually run one cycle — updater not needed, testing repo directly
    updates = queue_repo.pending_ack_updates()
    assert updates == []


def test_detects_position_change_after_dequeue(comment_repo, queue_repo):
    queue_repo.add_item('a', ack_position=0)
    queue_repo.add_item('b', ack_position=1)
    queue_repo.add_item('c', ack_position=2)

    # Remove 'a' — b moves to 0, c moves to 1
    queue_repo.remove_item('a')
    updates = queue_repo.pending_ack_updates()

    assert len(updates) == 2
    assert updates[0].audit_id == 'b'
    assert updates[0].current_position == 0
    assert updates[1].audit_id == 'c'
    assert updates[1].current_position == 1


def test_post_update_posts_and_records_ack(comment_repo, queue_repo):
    queue_repo.add_item('a', ack_id='ack-0', ack_position=None)
    updater = AckUpdater(queue_repo, comment_repo, rate_per_minute=60, poll_seconds=0.01)

    updates = queue_repo.pending_ack_updates()
    assert len(updates) == 1
    updater._post_update(updates[0])

    assert len(comment_repo.posts) == 1
    assert 'starting shortly' in comment_repo.posts[0]['message']
    assert len(comment_repo.deletes) == 1
    assert comment_repo.deletes[0]['comment_id'] == 'ack-0'
    assert len(queue_repo._ack_updates) == 1
    assert queue_repo._ack_updates[0]['position'] == 0


# ── End-to-end: start / stop thread ──────────────────────────────────

def test_start_stop_thread_runs_cleanly(comment_repo, queue_repo):
    updater = AckUpdater(queue_repo, comment_repo, rate_per_minute=60, poll_seconds=0.01)
    updater.start()
    assert updater._thread is not None
    assert updater._thread.is_alive()
    updater.stop()
    assert updater._thread is None


def test_thread_posts_updates_when_queue_moves(comment_repo, queue_repo):
    queue_repo.add_item('a', ack_id='ack-a', ack_position=0)
    queue_repo.add_item('b', ack_id='ack-b', ack_position=1)
    queue_repo.add_item('c', ack_id='ack-c', ack_position=2)

    updater = AckUpdater(queue_repo, comment_repo, rate_per_minute=60, poll_seconds=0.01)
    updater.start()

    try:
        # Simulate dequeue of 'a'
        queue_repo.remove_item('a')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if len(comment_repo.posts) >= 2:
                break
            time.sleep(0.02)
    finally:
        updater.stop()

    assert len(comment_repo.posts) >= 2
    positions = [p['message'] for p in comment_repo.posts]
    assert any('starting shortly' in m for m in positions)
    assert any('1 ahead of you' in m for m in positions)
