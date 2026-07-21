"""Background worker that posts queue-position ack updates at a low,
self-imposed rate.

Queries the AuditQueueRepository for queued items whose position has changed
since the last ack was posted, then posts one update per cycle if the rate
bucket allows.
"""

import logging
import threading
from typing import Optional

from figwatch.log_context import reset_audit_context, set_audit_context
from figwatch.ports import CommentRepository
from figwatch.providers.ai.rate_limit import TokenBucket
from figwatch.tracing import format_trace_line

logger = logging.getLogger(__name__)


def _position_message(trigger: str, position: int, trace_line: str) -> str:
    """Build the ack body for a given position. position=0 means 'starting shortly'."""
    name = trigger.lstrip('@')
    if position <= 0:
        return f'\u23f3 {name} audit queued \u2014 starting shortly\u2026{trace_line}'
    if position == 1:
        return f'\u23f3 {name} audit queued (1 ahead of you)\u2026{trace_line}'
    return f'\u23f3 {name} audit queued ({position} ahead of you)\u2026{trace_line}'


class AckUpdater:
    """Background thread that refreshes queued items' acks with their current
    queue position, capped at `rate_per_minute` writes.
    """

    def __init__(
        self,
        queue_repo,
        comment_repo: CommentRepository,
        rate_per_minute: int = 5,
        poll_seconds: float = 2.0,
    ):
        self._queue_repo = queue_repo
        self._comment_repo = comment_repo
        self._rate_per_minute = rate_per_minute
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._limiter: Optional[TokenBucket] = None
        if rate_per_minute > 0:
            self._limiter = TokenBucket(
                capacity=rate_per_minute,
                refill_per_second=rate_per_minute / 60,
            )

    # ── Public API ─────────────────────────────────────────────────────

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
                updates = self._queue_repo.pending_ack_updates()
                for update in updates:
                    if self._limiter is not None and not self._limiter.try_acquire():
                        break
                    self._post_update(update)
            except Exception:
                logger.exception('ack updater cycle crashed')
        logger.info('ack updater stopped')

    def _post_update(self, update) -> None:
        audit = update.audit
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
            trace_line = format_trace_line()
            new_message = _position_message(trigger_kw, update.current_position, trace_line)

            if update.ack_id:
                self._comment_repo.delete_comment(file_key, update.ack_id)
            new_ack_id = self._comment_repo.post_reply(
                file_key, audit.reply_to_id, new_message,
            )

            self._queue_repo.update_ack(
                update.audit_id, new_ack_id, update.current_position,
            )
            logger.info('ack.updated', extra={'position': update.current_position})
        except Exception:
            logger.exception('ack update post failed')
        finally:
            reset_audit_context(token)
