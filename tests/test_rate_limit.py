"""Tests for figwatch.providers.ai.rate_limit — TokenBucket."""

import threading
import pytest

from figwatch.providers.ai.rate_limit import TokenBucket


class FakeClock:
    """Deterministic clock + sleep for rate limiter tests."""

    def __init__(self):
        self.now_value = 1000.0
        self.slept = []

    def now(self):
        return self.now_value

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now_value += seconds


def _make_bucket(capacity, refill_per_second, clock=None):
    clock = clock or FakeClock()
    bucket = TokenBucket(
        capacity=capacity,
        refill_per_second=refill_per_second,
        now=clock.now,
        sleep=clock.sleep,
    )
    return bucket, clock


# ── Construction ──────────────────────────────────────────────────────

def test_invalid_capacity_raises():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_per_second=1)


def test_invalid_refill_raises():
    with pytest.raises(ValueError):
        TokenBucket(capacity=10, refill_per_second=0)


# ── Basic acquisition ─────────────────────────────────────────────────

def test_acquire_when_full_does_not_sleep():
    bucket, clock = _make_bucket(capacity=5, refill_per_second=1)
    bucket.acquire()
    assert clock.slept == []


def test_acquire_multiple_from_full():
    bucket, clock = _make_bucket(capacity=3, refill_per_second=1)
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()
    assert clock.slept == []


def test_acquire_blocks_when_empty():
    bucket, clock = _make_bucket(capacity=1, refill_per_second=1)
    bucket.acquire()  # drains
    bucket.acquire()  # must wait 1 second (1 token at 1/sec)
    assert clock.slept == [pytest.approx(1.0)]


def test_acquire_blocks_proportional_to_shortfall():
    bucket, clock = _make_bucket(capacity=2, refill_per_second=2)  # 2/sec
    bucket.acquire()
    bucket.acquire()  # drained
    bucket.acquire()  # needs 1 token at 2/sec → 0.5s
    assert clock.slept == [pytest.approx(0.5)]


# ── Refill over time ──────────────────────────────────────────────────

def test_tokens_refill_over_time():
    bucket, clock = _make_bucket(capacity=10, refill_per_second=5)
    # Drain completely
    for _ in range(10):
        bucket.acquire()
    assert clock.slept == []

    clock.now_value += 1.0  # 1 second elapses — 5 tokens refilled
    for _ in range(5):
        bucket.acquire()
    assert clock.slept == []  # no sleep needed, tokens were refilled


def test_refill_capped_at_capacity():
    bucket, clock = _make_bucket(capacity=3, refill_per_second=1)
    # Drain
    for _ in range(3):
        bucket.acquire()

    clock.now_value += 1000  # lots of time passes
    # Bucket should only hold capacity, not 1003 tokens
    for _ in range(3):
        bucket.acquire()
    assert clock.slept == []

    # 4th acquire must block
    bucket.acquire()
    assert len(clock.slept) == 1


# ── Multi-token acquires ──────────────────────────────────────────────

def test_acquire_multiple_tokens_at_once():
    bucket, clock = _make_bucket(capacity=5, refill_per_second=1)
    bucket.acquire(tokens=5)
    assert clock.slept == []


def test_acquire_multiple_blocks_when_insufficient():
    bucket, clock = _make_bucket(capacity=5, refill_per_second=2)
    bucket.acquire(tokens=5)  # drained
    bucket.acquire(tokens=4)  # needs 4 tokens at 2/sec → 2.0s
    assert clock.slept == [pytest.approx(2.0)]


# ── Thread safety ─────────────────────────────────────────────────────

def test_concurrent_acquire_stays_within_capacity():
    """10 threads each acquire once; only capacity+refilled count succeed immediately."""
    bucket = TokenBucket(capacity=3, refill_per_second=1e9)  # effectively unlimited refill

    errors = []

    def worker():
        try:
            bucket.acquire()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert errors == []
    assert all(not t.is_alive() for t in threads)
