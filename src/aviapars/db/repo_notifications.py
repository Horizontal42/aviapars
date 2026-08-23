from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class NotificationsRepo:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def last_sent_for_itinerary(self, itinerary_hash: str) -> datetime | None:
        async with self._db.execute(
            "SELECT MAX(sent_at) FROM notifications_sent WHERE itinerary_hash = ?",
            (itinerary_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    async def count_today_for_route(self, route_id: int, *, since: datetime) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM notifications_sent WHERE route_id = ? AND sent_at >= ?",
            (route_id, since.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def record(
        self,
        *,
        route_id: int,
        dedupe_key: str,
        itinerary_hash: str,
        price_minor: int,
        mode: str,
        depart_date: str,
        tier_at_send: str,
        sent_at: datetime | None = None,
    ) -> None:
        # sent_at is passed explicitly (not the schema's `datetime('now')` default) so its
        # format matches every other timestamp this repo reads back with datetime.fromisoformat
        # and compares against Python-generated ISO strings elsewhere (§4 timestamps are UTC
        # instants everywhere) — SQLite's own datetime('now') uses a different, incompatible
        # string format (space separator, no offset) that silently breaks both.
        # The caller's own clock when it has one: the notifier decides "may I send" and
        # "record that I sent" against the same instant, and a row stamped from a second
        # clock is a row the cooldown compares against something it never checked.
        stamp = (sent_at or datetime.now(timezone.utc)).isoformat()
        await self._db.execute(
            """
            INSERT INTO notifications_sent
                (route_id, dedupe_key, itinerary_hash, price_minor, mode, depart_date, tier_at_send, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (route_id, dedupe_key, itinerary_hash, price_minor, mode, depart_date, tier_at_send, stamp),
        )
        await self._db.commit()
