from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

_FIELDS = "route_id, pending_day, price_minor, currency, itinerary_hash, mode, depart_date, tier_at_send, card_text"


@dataclass(slots=True)
class PendingDigest:
    route_id: int
    day: str  # user's local digest day (see notify/notifier.digest_day)
    price_minor: int
    currency: str
    itinerary_hash: str
    mode: str
    depart_date: str
    tier_at_send: str
    card_text: str


class DigestsRepo:
    """Durable accumulator behind `routes.notification_mode = 'digest'` (§9).

    DB-backed for the same reason the quiet-hours deferral is: a digest that
    only exists in process memory silently loses a day's best price to any
    restart, and this bot restarts on deploy.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def stage(
        self,
        *,
        route_id: int,
        day: str,
        price_minor: int,
        currency: str,
        itinerary_hash: str,
        mode: str,
        depart_date: str,
        tier_at_send: str,
        card_text: str,
    ) -> bool:
        """Records this itinerary as the day's best if it beats what's staged.
        Returns True when it actually replaced something (or opened a new day).

        The WHERE clause is what makes "best of the day" work without a read-
        modify-write race: a new digest day always overwrites (the previous day's
        row has either been claimed or is stale), and within one day only a
        strictly cheaper price does.
        """
        cursor = await self._db.execute(
            f"""
            INSERT INTO route_digests ({_FIELDS}, staged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_id) DO UPDATE SET
                pending_day = excluded.pending_day,
                price_minor = excluded.price_minor,
                currency = excluded.currency,
                itinerary_hash = excluded.itinerary_hash,
                mode = excluded.mode,
                depart_date = excluded.depart_date,
                tier_at_send = excluded.tier_at_send,
                card_text = excluded.card_text,
                staged_at = excluded.staged_at
            WHERE route_digests.pending_day IS NOT excluded.pending_day
               OR excluded.price_minor < route_digests.price_minor
            """,
            (
                route_id,
                day,
                price_minor,
                currency,
                itinerary_hash,
                mode,
                depart_date,
                tier_at_send,
                card_text,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def pending(self) -> list[PendingDigest]:
        async with self._db.execute(
            f"SELECT {_FIELDS} FROM route_digests WHERE pending_day IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            PendingDigest(
                route_id=row[0],
                day=row[1],
                price_minor=row[2],
                currency=row[3],
                itinerary_hash=row[4],
                mode=row[5],
                depart_date=row[6],
                tier_at_send=row[7],
                card_text=row[8],
            )
            for row in rows
        ]

    async def claim(self, route_id: int, day: str) -> bool:
        """Takes ownership of one day's digest, returning False if someone
        already has it. This conditional UPDATE is the whole double-send guard:
        the scheduler ticks every 60s and a due window stays due for the rest of
        the day, so "did I already send this?" cannot be answered from a flag
        read a moment earlier — it has to be the write itself that decides.

        Claiming before delivering (not after) is the same trade the quiet-hours
        path already makes: a crash between claim and send loses one digest,
        which is much better than a restart loop sending the same one repeatedly.
        """
        cursor = await self._db.execute(
            "UPDATE route_digests SET pending_day = NULL, last_sent_day = ? WHERE route_id = ? AND pending_day = ?",
            (day, route_id, day),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def clear(self, route_id: int) -> None:
        """Used when a route leaves digest mode — a staged best from yesterday
        must not surface days later if the user switches back."""
        await self._db.execute("DELETE FROM route_digests WHERE route_id = ?", (route_id,))
        await self._db.commit()
