from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from aviapars.db.models import Route

_COLUMNS = (
    "id, user_id, title, origin_place_id, dest_place_id, mode_pref, date_mode, fixed_dates, "
    "window_start_date, window_start_offset_days, window_length_days, date_cursor, "
    "max_price, max_price_currency, max_price_by_mode, pax, "
    "allow_hub_routing, hub_place_ids, max_hops, max_transfer_min, max_total_hours, "
    "poll_interval_min, notification_mode, active, next_run_at, last_run_at, created_at"
)

_SELECT_GET = "SELECT " + _COLUMNS + " FROM routes WHERE id = ?"
_SELECT_LIST = "SELECT " + _COLUMNS + " FROM routes WHERE user_id = ? ORDER BY created_at DESC"
_SELECT_DUE = (
    "SELECT " + _COLUMNS + " FROM routes\n"
    "WHERE active = 1 AND (next_run_at IS NULL OR next_run_at <= ?)\n"
    "ORDER BY next_run_at ASC\n"
    "LIMIT ?"
)

NOTIFICATION_MODES = ("instant", "digest")


def _row_to_route(row: aiosqlite.Row) -> Route:
    return Route(
        id=row[0],
        user_id=row[1],
        title=row[2],
        origin_place_id=row[3],
        dest_place_id=row[4],
        mode_pref=row[5],
        date_mode=row[6],
        fixed_dates=json.loads(row[7]) if row[7] else None,
        window_start_date=row[8],
        window_start_offset_days=row[9],
        window_length_days=row[10],
        date_cursor=row[11],
        max_price=row[12],
        max_price_currency=row[13],
        max_price_by_mode=json.loads(row[14]) if row[14] else None,
        pax=row[15],
        allow_hub_routing=bool(row[16]),
        hub_place_ids=json.loads(row[17]) if row[17] else None,
        max_hops=row[18],
        max_transfer_min=row[19],
        max_total_hours=row[20],
        poll_interval_min=row[21],
        notification_mode=row[22],
        active=bool(row[23]),
        next_run_at=row[24],
        last_run_at=row[25],
        created_at=row[26],
    )


class RoutesRepo:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create(
        self,
        *,
        user_id: int,
        title: str,
        origin_place_id: int,
        dest_place_id: int,
        mode_pref: str,
        date_mode: str,
        fixed_dates: list[str] | None,
        window_start_date: str | None = None,
        window_start_offset_days: int | None = None,
        window_length_days: int | None = None,
        max_price: int,
        max_price_currency: str,
        max_price_by_mode: dict[str, int] | None = None,
        pax: int = 1,
        allow_hub_routing: bool = True,
        hub_place_ids: list[int] | None = None,
        max_hops: int = 1,
        max_transfer_min: int | None = None,
        max_total_hours: float | None = None,
        poll_interval_min: int = 60,
        notification_mode: str = "instant",
    ) -> Route:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            """
            INSERT INTO routes (
                user_id, title, origin_place_id, dest_place_id, mode_pref, date_mode, fixed_dates,
                window_start_date, window_start_offset_days, window_length_days,
                max_price, max_price_currency, max_price_by_mode, pax,
                allow_hub_routing, hub_place_ids, max_hops, max_transfer_min, max_total_hours,
                poll_interval_min, notification_mode, active, next_run_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                user_id,
                title,
                origin_place_id,
                dest_place_id,
                mode_pref,
                date_mode,
                json.dumps(fixed_dates) if fixed_dates else None,
                window_start_date,
                window_start_offset_days,
                window_length_days,
                max_price,
                max_price_currency,
                json.dumps(max_price_by_mode) if max_price_by_mode else None,
                pax,
                int(allow_hub_routing),
                json.dumps(hub_place_ids) if hub_place_ids else None,
                max_hops,
                max_transfer_min,
                max_total_hours,
                poll_interval_min,
                notification_mode,
                now,  # next_run_at = now => picked up on the next tick
                now,
            ),
        )
        await self._db.commit()
        route = await self.get(cursor.lastrowid)  # type: ignore[arg-type]
        assert route is not None
        return route

    async def get(self, route_id: int) -> Route | None:
        async with self._db.execute(_SELECT_GET, (route_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_route(row) if row else None

    async def list_for_user(self, user_id: int) -> list[Route]:
        async with self._db.execute(
            _SELECT_LIST, (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_route(row) for row in rows]

    async def due(self, *, now: datetime, limit: int = 50) -> list[Route]:
        async with self._db.execute(
            _SELECT_DUE,
            (now.isoformat(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_route(row) for row in rows]

    async def mark_run(self, route_id: int, *, next_run_at: datetime, ran_at: datetime) -> None:
        await self._db.execute(
            "UPDATE routes SET next_run_at = ?, last_run_at = ? WHERE id = ?",
            (next_run_at.isoformat(), ran_at.isoformat(), route_id),
        )
        await self._db.commit()

    async def set_date_cursor(self, route_id: int, cursor: int) -> None:
        await self._db.execute("UPDATE routes SET date_cursor = ? WHERE id = ?", (cursor, route_id))
        await self._db.commit()

    async def set_notification_mode(self, route_id: int, mode: str) -> None:
        assert mode in NOTIFICATION_MODES
        await self._db.execute("UPDATE routes SET notification_mode = ? WHERE id = ?", (mode, route_id))
        await self._db.commit()

    async def set_poll_interval(self, route_id: int, minutes: int) -> None:
        await self._db.execute("UPDATE routes SET poll_interval_min = ? WHERE id = ?", (minutes, route_id))
        await self._db.commit()

    async def set_active(self, route_id: int, active: bool) -> None:
        await self._db.execute("UPDATE routes SET active = ? WHERE id = ?", (int(active), route_id))
        await self._db.commit()

    async def delete(self, route_id: int) -> None:
        await self._db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await self._db.commit()
