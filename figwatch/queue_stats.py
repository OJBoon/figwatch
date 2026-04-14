"""Instrumented work queue with depth tracking and per-item wait time."""

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QueuedItem:
    """Wrapper around a WorkItem tracking queue/processing metadata."""
    item: Any
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
    """Thin wrapper over queue.Queue with depth counters and wait-time stamping.

    `put()` increments enqueued and stamps enqueued_at onto the wrapped
    QueuedItem (or leaves existing timestamp alone for retries that re-put).
    `get()` increments dequeued, computes waited_seconds, and returns the item.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._stats_lock = threading.Lock()
        self._stats = QueueStats()

    def put(self, queued: Any) -> None:
        with self._stats_lock:
            self._stats.enqueued += 1
        self._queue.put(queued)

    def get(self, timeout: Optional[float] = None) -> Optional[QueuedItem]:
        try:
            queued = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._stats_lock:
            self._stats.dequeued += 1
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
