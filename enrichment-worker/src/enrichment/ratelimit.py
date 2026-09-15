"""Client-side rate limiting.

The benchmark run that motivated this file hit HTTP 429 on two listings out of
thirty-one, which the retry path correctly routed to the dead-letter queue --
correct behaviour, but a listing lost to a quota limit we could have respected
is pure waste. Pacing requests locally is cheaper than retrying them remotely.

Separate buckets per operation, because extraction and embedding are billed and
limited independently: one shared limiter would throttle embeddings to the much
lower extraction rate for no reason.
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """A simple thread-safe token bucket."""

    def __init__(self, rate_per_minute: float, burst: int | None = None):
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_minute)))
        self.tokens = self.capacity
        self.refill_per_second = rate_per_minute / 60.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available. Returns how long it waited."""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.refill_per_second)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                # How long until one token exists.
                deficit = 1.0 - self.tokens
                sleep_for = deficit / self.refill_per_second if self.refill_per_second else 1.0

            sleep_for = min(sleep_for, 5.0)
            time.sleep(sleep_for)
            waited += sleep_for
