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

    def _refill(self) -> None:
        now = self._now()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now
