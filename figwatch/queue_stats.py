"""Instrumented work queue with depth tracking, wait-time stamping, and a
FIFO mirror used by the ack updater to compute per-audit queue positions.
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class QueuedItem:
    """Wraps an Audit with queue/processing metadata.

    `ack_id` is mutable: the AckUpdater posts a new ack and writes the new
    ID back onto this object while the item is still in the queue. Workers
    only read ack_id after get() returns the item, at which point the
    updater is guaranteed to have stopped touching it (via cancel()).
    """
    audit: Any
    ack_id: Optional[str]
    audit_id: str
    enqueued_at: float = field(default_factory=time.monotonic)
    attempt: int = 1
    waited_seconds: float = 0.0


@dataclass
class QueueStats:
    enqueued: int = 0
    dequeued: int = 0

    @property
    def depth(self) -> int:
        return self.enqueued - self.dequeued


class InstrumentedQueue:
    """Thin wrapper over queue.Queue with depth counters, wait-time stamping,
    and a parallel FIFO mirror (`snapshot_order`) so the AckUpdater can walk
    current queue contents to compute positions.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._stats_lock = threading.Lock()
        self._stats = QueueStats()
        # FIFO mirror keyed by audit_id for O(1) find and O(n) position lookup.
        # Duplicate audit_ids are not supported — audit_ids are UUID-derived
        # and should always be unique per run.
        self._ordered_ids: List[str] = []
        self._items_by_id: dict = {}

    def put(self, queued: Any) -> None:
        with self._stats_lock:
            self._stats.enqueued += 1
            if isinstance(queued, QueuedItem):
                self._ordered_ids.append(queued.audit_id)
                self._items_by_id[queued.audit_id] = queued
        self._queue.put(queued)

    def get(self, timeout: Optional[float] = None) -> Optional[QueuedItem]:
        try:
            queued = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._stats_lock:
            self._stats.dequeued += 1
            if isinstance(queued, QueuedItem):
                try:
                    self._ordered_ids.remove(queued.audit_id)
                except ValueError:
                    pass
                self._items_by_id.pop(queued.audit_id, None)
        if isinstance(queued, QueuedItem):
            queued.waited_seconds = time.monotonic() - queued.enqueued_at
        return queued

    def task_done(self) -> None:
        self._queue.task_done()

    def stats(self) -> QueueStats:
        with self._stats_lock:
            return QueueStats(
                enqueued=self._stats.enqueued,
                dequeued=self._stats.dequeued,
            )

    @property
    def depth(self) -> int:
        return self.stats().depth

    def qsize(self) -> int:
        """Pass-through to underlying queue.Queue.qsize() — approximate."""
        return self._queue.qsize()

    def snapshot_order(self) -> List[QueuedItem]:
        """Return a FIFO snapshot of currently queued items.

        Used by AckUpdater to compute per-audit queue positions. O(n) copy
        under the stats lock.
        """
        with self._stats_lock:
            return [
                self._items_by_id[aid]
                for aid in self._ordered_ids
                if aid in self._items_by_id
            ]

    def find(self, audit_id: str) -> Optional[QueuedItem]:
        """Return the QueuedItem for a given audit_id if still queued."""
        with self._stats_lock:
            return self._items_by_id.get(audit_id)
