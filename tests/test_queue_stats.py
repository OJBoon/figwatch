"""Tests for figwatch.queue_stats — InstrumentedQueue + QueuedItem."""

import threading
import time

import pytest

from figwatch.queue_stats import InstrumentedQueue, QueuedItem


def _queued(audit_id='a3f9', attempt=1):
    return QueuedItem(item='stub', ack_id=None, audit_id=audit_id, attempt=attempt)


# ── Basic put / get ──────────────────────────────────────────────────

def test_put_increments_enqueued():
    q = InstrumentedQueue()
    q.put(_queued())
    assert q.stats().enqueued == 1
    assert q.stats().dequeued == 0
    assert q.depth == 1


def test_get_increments_dequeued():
    q = InstrumentedQueue()
    q.put(_queued())
    item = q.get(timeout=1)
    assert item is not None
    assert q.stats().dequeued == 1
    assert q.depth == 0


def test_get_on_empty_queue_returns_none():
    q = InstrumentedQueue()
    assert q.get(timeout=0.05) is None
    assert q.stats().dequeued == 0


def test_depth_tracks_puts_minus_gets():
    q = InstrumentedQueue()
    for _ in range(5):
        q.put(_queued())
    assert q.depth == 5
    q.get(timeout=1)
    q.get(timeout=1)
    assert q.depth == 3


# ── Wait time ────────────────────────────────────────────────────────

def test_waited_seconds_populated_on_get():
    q = InstrumentedQueue()
    q.put(_queued())
    time.sleep(0.05)
    item = q.get(timeout=1)
    assert item.waited_seconds >= 0.05
    assert item.waited_seconds < 1.0


def test_waited_seconds_near_zero_for_immediate_get():
    q = InstrumentedQueue()
    q.put(_queued())
    item = q.get(timeout=1)
    assert item.waited_seconds < 0.1


# ── Ordering (FIFO) ──────────────────────────────────────────────────

def test_fifo_order_preserved():
    q = InstrumentedQueue()
    q.put(_queued(audit_id='first'))
    q.put(_queued(audit_id='second'))
    q.put(_queued(audit_id='third'))

    assert q.get(timeout=1).audit_id == 'first'
    assert q.get(timeout=1).audit_id == 'second'
    assert q.get(timeout=1).audit_id == 'third'


# ── Concurrency ──────────────────────────────────────────────────────

def test_concurrent_producers_and_consumers():
    q = InstrumentedQueue()
    produced = 50
    consumed = []
    consumed_lock = threading.Lock()

    def producer():
        for i in range(produced):
            q.put(_queued(audit_id=f'p{i}'))

    def consumer():
        while True:
            item = q.get(timeout=0.5)
            if item is None:
                return
            with consumed_lock:
                consumed.append(item.audit_id)

    producer_thread = threading.Thread(target=producer)
    consumer_threads = [threading.Thread(target=consumer) for _ in range(3)]

    producer_thread.start()
    for t in consumer_threads:
        t.start()

    producer_thread.join()
    for t in consumer_threads:
        t.join(timeout=5)

    assert len(consumed) == produced
    assert q.stats().enqueued == produced
    assert q.stats().dequeued == produced
    assert q.depth == 0


def test_stats_snapshot_does_not_share_state():
    q = InstrumentedQueue()
    q.put(_queued())
    snapshot1 = q.stats()
    q.put(_queued())
    snapshot2 = q.stats()
    assert snapshot1.enqueued == 1
    assert snapshot2.enqueued == 2


# ── QueuedItem ───────────────────────────────────────────────────────

def test_queued_item_defaults():
    item = QueuedItem(item='work', ack_id=None, audit_id='a3f9')
    assert item.attempt == 1
    assert item.waited_seconds == 0.0
    assert item.enqueued_at > 0
