"""Background worker that posts queue-position ack updates at a low,
self-imposed rate.

Design (per approved plan):

- Skills/analysis path is never blocked by ack update work
- Updater runs in its own thread with its own TokenBucket
- Per-audit coalescing: scheduling a new update for an audit_id replaces any
  pending entry, so only the latest known position is ever posted
- Workers call cancel(audit_id) at dequeue time to stop pending updates from
  racing with the worker's own ack lifecycle

The updater polls InstrumentedQueue.snapshot_order() on each cycle. For each
item whose current position differs from its last-displayed position, a
PendingUpdate is inserted/replaced in the pending dict. Then one pending
update is popped and posted if the rate bucket has a token.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from figwatch.log_context import set_audit_context, reset_audit_context
from figwatch.ports import CommentRepository
from figwatch.providers.ai.rate_limit import TokenBucket
from figwatch.queue_stats import InstrumentedQueue, QueuedItem

logger = logging.getLogger(__name__)


@dataclass
class PendingUpdate:
    audit_id: str
    new_position: int
    queued: QueuedItem  # mutable — we write new ack_id back here after posting


def _position_message(trigger: str, position: int) -> str:
    """Build the ack body for a given position. position=0 means 'starting shortly'."""
    name = trigger.lstrip('@')
    if position <= 0:
        return f'\u23f3 {name} audit queued \u2014 starting shortly\u2026'
    if position == 1:
        return f'\u23f3 {name} audit queued (1 ahead of you)\u2026'
    return f'\u23f3 {name} audit queued ({position} ahead of you)\u2026'


class AckUpdater:
    """Background thread that refreshes queued items' acks with their current
    queue position, capped at `rate_per_minute` writes.

    Cheap cancellation: workers call cancel(audit_id) at dequeue time, which
    removes the audit from both the displayed and pending maps under lock.
    """

    def __init__(
        self,
        work_queue: InstrumentedQueue,
        comment_repo: CommentRepository,
        rate_per_minute: int = 5,
        poll_seconds: float = 2.0,
    ):
        self._queue = work_queue
        self._comment_repo = comment_repo
        self._rate_per_minute = rate_per_minute
        self._poll_seconds = poll_seconds
        # Maps audit_id → last-posted position. An audit with no entry here
        # has never had its position displayed; when we first see it, we
        # record its initial position (from track_initial) and never fire
        # a redundant update for it.
        self._displayed: Dict[str, int] = {}
        self._pending: Dict[str, PendingUpdate] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._limiter: Optional[TokenBucket] = None
        if rate_per_minute > 0:
            self._limiter = TokenBucket(
                capacity=rate_per_minute,
                refill_per_second=rate_per_minute / 60,
            )

    # ── Public API ─────────────────────────────────────────────────────

    def track_initial(self, audit_id: str, position: int) -> None:
        """Record the initial displayed position so we don't re-post it."""
        with self._lock:
            self._displayed[audit_id] = position

    def cancel(self, audit_id: str) -> None:
        """Called by the worker at dequeue — drops any tracking state for
        the audit so the updater won't touch its ack.
        """
        with self._lock:
            self._pending.pop(audit_id, None)
            self._displayed.pop(audit_id, None)

    def start(self) -> None:
        if self._rate_per_minute <= 0:
            logger.info('ack updater disabled (rate=0)')
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name='figwatch-ack-updater',
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ── Internals ──────────────────────────────────────────────────────

    def _run(self) -> None:
        logger.info('ack updater started',
                    extra={'rate_per_minute': self._rate_per_minute,
                           'poll_seconds': self._poll_seconds})
        while not self._stop.wait(timeout=self._poll_seconds):
            try:
                self._refresh_pending()
                self._post_one()
            except Exception:
                logger.exception('ack updater cycle crashed')
        logger.info('ack updater stopped')

    def _refresh_pending(self) -> None:
        """Walk the queue, schedule/coalesce updates for items whose position
        has changed relative to the last displayed value.
        """
        snapshot = self._queue.snapshot_order()
        with self._lock:
            live_ids = {q.audit_id for q in snapshot}
            # Clean up tracking state for items that have left the queue
            # without going through cancel() (shouldn't happen in practice,
            # but defends against bugs).
            for stale in list(self._displayed.keys()):
                if stale not in live_ids:
                    self._displayed.pop(stale, None)
                    self._pending.pop(stale, None)

            for idx, queued in enumerate(snapshot):
                # Position is N-ahead-of-you, where N = items in front.
                # idx 0 = at the head → 0 ahead.
                position = idx
                last = self._displayed.get(queued.audit_id)
                if last is None:
                    # We haven't seen this item yet. Record its current
                    # position without firing an update — the webhook
                    # handler already posted the initial ack at its
                    # initial position.
                    self._displayed[queued.audit_id] = position
                    continue
                if last == position:
                    continue
                # Position moved — schedule/replace a pending update.
                self._pending[queued.audit_id] = PendingUpdate(
                    audit_id=queued.audit_id,
                    new_position=position,
                    queued=queued,
                )

    def _post_one(self) -> None:
        """Pop a single pending update and post it if the rate bucket allows."""
        with self._lock:
            if not self._pending:
                return
            # Pop in insertion order (approximates FIFO). Coalescing already
            # guarantees we post the latest state for each audit.
            audit_id = next(iter(self._pending))
            update = self._pending.pop(audit_id)

        # Check the limiter without blocking. If empty, re-queue and wait.
        if self._limiter is not None and not self._limiter.try_acquire():
            with self._lock:
                # Only re-queue if nothing newer has been scheduled since.
                self._pending.setdefault(update.audit_id, update)
            return

        # Double-check the audit is still in the queue before posting —
        # a worker may have dequeued it between our pop and now.
        queued = update.queued
        if self._queue.find(update.audit_id) is None:
            logger.debug('ack update skipped — audit no longer queued',
                         extra={'audit': update.audit_id})
            return

        audit = queued.audit
        trigger_kw = audit.trigger_match.trigger.keyword
        file_key = audit.comment.file_key
        node_id = audit.comment.node_id

        token = set_audit_context(
            audit=update.audit_id,
            trigger=trigger_kw,
            node=node_id,
            file=file_key,
        )
        try:
            new_message = _position_message(trigger_kw, update.new_position)
            old_ack_id = queued.ack_id

            # Delete old ack, post new one
            if old_ack_id:
                self._comment_repo.delete_comment(file_key, old_ack_id)
            new_ack_id = self._comment_repo.post_reply(
                file_key, audit.reply_to_id, new_message,
            )

            # Write the new ack_id back onto the QueuedItem so the worker
            # picks it up when/if it dequeues. If the worker dequeued
            # concurrently, this write is to a detached object — harmless.
            queued.ack_id = new_ack_id
            with self._lock:
                self._displayed[update.audit_id] = update.new_position
            logger.info('ack.updated',
                        extra={'position': update.new_position})
        except Exception:
            logger.exception('ack update post failed')
        finally:
            reset_audit_context(token)
