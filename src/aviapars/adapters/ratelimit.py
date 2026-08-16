from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from aviapars.adapters.base import RateLimitPolicy


class RateLimiter:
    """Per-source token-bucket pacing (§6). One bucket per source_id, keyed by
    the minimum interval between requests the adapter declares in its
    RateLimitPolicy. Daily caps reset on UTC midnight, tracked separately by
    the caller (source_health.requests_today in the DB) — this class only
    knows about the in-process pacing interval.
    """

    def __init__(self) -> None:
        self._last_request_at: dict[str, float] = defaultdict(lambda: 0.0)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, source_id: str, policy: RateLimitPolicy, max_wait_s: float | None = None) -> bool:
        """Blocks until the source's min_interval_s has elapsed since its last
        request, then reserves the slot. If max_wait_s is given (interactive
        lane, §6) and the wait would exceed it, returns False without
        reserving anything — caller should skip this source for the round.
        """
        lock = self._locks[source_id]
        async with lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at[source_id]
            wait = policy.min_interval_s - elapsed
            if wait > 0:
                if max_wait_s is not None and wait > max_wait_s:
                    return False
                await asyncio.sleep(wait)
            self._last_request_at[source_id] = time.monotonic()
            return True
