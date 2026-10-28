"""Thread-safe token bucket rate limiter."""

import threading
import time


class TokenBucket:
    """Token bucket rate limiter.

    Refills at refill_per_second up to capacity. acquire() blocks until the
    requested tokens are available.

    Time and sleep are injectable so tests can drive the limiter deterministically.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        now=time.monotonic,
        sleep=time.sleep,
    ):
        if capacity <= 0:
            raise ValueError('capacity must be positive')
        if refill_per_second <= 0:
            raise ValueError('refill_per_second must be positive')
        self._capacity = capacity
        self._refill_rate = refill_per_second
        self._tokens = float(capacity)
        self._now = now
        self._sleep = sleep
        self._last_refill = now()
        self._lock = threading.Lock()

    @classmethod
    def per_minute(cls, rpm: int, **kwargs) -> 'TokenBucket':
        """A bucket allowing `rpm` requests a minute, with a one-minute burst."""
        return cls(capacity=rpm, refill_per_second=rpm / 60, **kwargs)

    def acquire(self, tokens: int = 1) -> None:
        """Block until `tokens` tokens are available, then consume them."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                needed = tokens - self._tokens
                wait_seconds = needed / self._refill_rate
            # Release the lock before sleeping so other threads can still
            # check the bucket while we wait.
            self._sleep(wait_seconds)

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking token acquire. Returns True on success, False if empty."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def drain(self, seconds: float) -> None:
        """Empty the bucket so the next acquire waits about `seconds` (e.g. after a 429)."""
        with self._lock:
            self._refill()
            self._tokens = -(self._refill_rate * seconds)

    def _refill(self) -> None:
        now = self._now()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now
