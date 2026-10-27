"""Per-identity token-bucket rate limit for data requests.

Bounds how much load one authenticated identity can put on the shared
database (statement timeouts bound each query; this bounds how many).
In-memory and per-process: adequate for a single-instance demo.
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, burst: int, per_second: float) -> None:
        self._burst = float(burst)
        self._rate = per_second
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True
