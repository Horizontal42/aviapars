from __future__ import annotations

from datetime import datetime, timedelta

from aviapars.db.repo_notifications import NotificationsRepo


class DedupeGate:
    """Anti-noise (§9): same itinerary_hash -> max 1 notification per cooldown
    window; per-route daily cap. Both DB-backed so limits survive a restart.
    """

    def __init__(self, repo: NotificationsRepo, *, cooldown_h: int, daily_cap: int) -> None:
        self._repo = repo
        self._cooldown_h = cooldown_h
        self._daily_cap = daily_cap

    async def can_send(
        self,
        *,
        route_id: int,
        itinerary_hash: str,
        now: datetime,
        day_start_utc: datetime,
    ) -> bool:
        last = await self._repo.last_sent_for_itinerary(itinerary_hash)
        if last is not None and (now - last) < timedelta(hours=self._cooldown_h):
            return False
        count = await self._repo.count_today_for_route(route_id, since=day_start_utc)
        return count < self._daily_cap
