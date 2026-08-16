from __future__ import annotations

import aiosqlite

from aviapars.db.models import Place


def _row_to_place(row: aiosqlite.Row) -> Place:
    return Place(
        id=row[0],
        kind=row[1],
        name_ru=row[2],
        name_en=row[3],
        iata=row[4],
        yandex_code=row[5],
        rzd_code=row[6],
        city_id=row[7],
        country=row[8],
        region=row[9],
        lat=row[10],
        lon=row[11],
        popularity=row[12],
        is_hub_candidate=bool(row[13]),
    )


_COLUMNS = (
    "id, kind, name_ru, name_en, iata, yandex_code, rzd_code, city_id, country, region, lat, lon, "
    "popularity, is_hub_candidate"
)


class PlacesRepo:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get(self, place_id: int) -> Place | None:
        async with self._db.execute(f"SELECT {_COLUMNS} FROM places WHERE id = ?", (place_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_place(row) if row else None

    async def search_by_name(self, query: str, *, limit: int = 6) -> list[Place]:
        """Sub-millisecond local lookup backing the /add and /search place picker
        (§10) — no external API call. Simple substring match, case-insensitive;
        ranked by popularity so a well-known city outranks an obscure station
        with a similar name.
        """
        like = f"%{query.strip()}%"
        async with self._db.execute(
            f"""
            SELECT {_COLUMNS} FROM places
            WHERE name_ru LIKE ? OR name_en LIKE ? OR iata = ?
            ORDER BY popularity DESC
            LIMIT ?
            """,
            (like, like, query.strip().upper(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_place(row) for row in rows]

    async def hub_candidates_near(self, origin: Place, dest: Place, *, limit: int = 3) -> list[Place]:
        """Dynamic hub discovery (§8) — no hardcoded city list. Cheap proxy for
        "near the route": same country as either endpoint, ranked by
        popularity. Good enough while the seed data is RU-only (§4); a real
        great-circle-deviation filter is worth adding once places span
        multiple countries and "same country" stops being a tight enough
        filter on its own.
        """
        async with self._db.execute(
            f"""
            SELECT {_COLUMNS} FROM places
            WHERE is_hub_candidate = 1
              AND country IN (?, ?)
              AND id NOT IN (?, ?)
            ORDER BY popularity DESC
            LIMIT ?
            """,
            (origin.country, dest.country, origin.id, dest.id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_place(row) for row in rows]
