from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

import aiosqlite

from aviapars.adapters.base import Offer


class ObservationsRepo:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def record(self, route_id: int, offer: Offer) -> None:
        depart_date = min(leg.depart_utc for leg in offer.legs).date().isoformat()
        return_date = None
        legs_json = json.dumps(
            [
                {
                    "mode": leg.mode.value,
                    "carrier": leg.carrier,
                    "number": leg.number,
                    "depart_utc": leg.depart_utc.isoformat(),
                    "arrive_utc": leg.arrive_utc.isoformat() if leg.arrive_utc else None,
                    "time_precision": leg.time_precision,
                }
                for leg in offer.legs
            ]
        )
        await self._db.execute(
            """
            INSERT INTO price_observations (
                route_id, source_id, mode, depart_date, return_date, price_minor, currency,
                itinerary_hash, legs_json, deep_link, observed_at, source_cached_at, expires_at,
                tier, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_id,
                offer.source_id,
                offer.legs[0].mode.value,
                depart_date,
                return_date,
                offer.price_minor,
                offer.currency,
                offer.itinerary_hash,
                legs_json,
                offer.deep_link,
                offer.observed_at.isoformat(),
                offer.source_cached_at.isoformat() if offer.source_cached_at else None,
                offer.expires_at.isoformat() if offer.expires_at else None,
                offer.tier.value,
                offer.confidence,
            ),
        )
        await self._db.execute(
            """
            INSERT INTO best_known (route_id, source_id, mode, depart_date, price_minor, itinerary_hash, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_id, source_id, mode, depart_date) DO UPDATE SET
                price_minor = excluded.price_minor,
                itinerary_hash = excluded.itinerary_hash,
                observed_at = excluded.observed_at
            WHERE excluded.price_minor <= best_known.price_minor
            """,
            (
                route_id,
                offer.source_id,
                offer.legs[0].mode.value,
                depart_date,
                offer.price_minor,
                offer.itinerary_hash,
                offer.observed_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def stats(self, route_id: int, mode: str, *, window_days: int = 30) -> dict[str, float | int | None]:
        """Powers the pre-threshold "typical prices" screen (§9). Computed on
        read rather than kept in route_stats incrementally — simplest thing
        that works at this data volume (a personal bot's route history is a
        few thousand rows at most, see §3)."""
        cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
        async with self._db.execute(
            "SELECT price_minor, observed_at FROM price_observations WHERE route_id = ? AND mode = ?",
            (route_id, mode),
        ) as cursor:
            rows = await cursor.fetchall()
        prices = [
            row[0]
            for row in rows
            if datetime.fromisoformat(row[1]).timestamp() >= cutoff
        ]
        if not prices:
            return {"min": None, "median": None, "p25": None, "max": None, "count": 0}
        prices.sort()
        return {
            "min": prices[0],
            "median": statistics.median(prices),
            "p25": prices[len(prices) // 4],
            "max": prices[-1],
            "count": len(prices),
        }

    async def prune_older_than(self, days: int = 90) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cursor = await self._db.execute("DELETE FROM price_observations WHERE observed_at < ?", (cutoff_iso,))
        await self._db.commit()
        return cursor.rowcount
