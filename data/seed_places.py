"""Seeds `places` from Travelpayouts' public cities/airports data dump
(§4: "seeded from Travelpayouts + Yandex station lists, filtered to the
country list in config"). This is the same source already integrated as the
`travelpayouts` adapter, not a new one — no reverse-engineering involved,
just two public JSON files: no auth, no rate limit documented.

Idempotent: upserts on (kind, iata) via the unique index from migration 002,
so re-running refreshes data instead of duplicating rows.

Rail stations (Yandex station list) are not seeded here yet — that lands
with the yandex_rasp adapter (Phase C).

Run: python -m aviapars.db.migrate && python data/seed_places.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aviapars.config import settings  # noqa: E402
from aviapars.db.engine import connect  # noqa: E402

CITIES_URL = "http://api.travelpayouts.com/data/ru/cities.json"
AIRPORTS_URL = "http://api.travelpayouts.com/data/ru/airports.json"


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> list[dict[str, Any]]:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        data: list[dict[str, Any]] = await resp.json(content_type=None)
        return data


async def main() -> None:
    countries = set(settings.seed_countries)
    async with aiohttp.ClientSession() as session:
        cities = await _fetch_json(session, CITIES_URL)
        airports = await _fetch_json(session, AIRPORTS_URL)

    cities = [c for c in cities if c.get("country_code") in countries and c.get("code")]
    airports = [
        a
        for a in airports
        if a.get("country_code") in countries
        and a.get("code")
        and a.get("iata_type") == "airport"
        and a.get("flightable")
    ]

    # Multi-airport cities look like natural interchange points — the closest honest proxy
    # for "hub" we can derive from this data alone (no real connectivity/traffic signal here).
    airports_per_city: dict[str, int] = {}
    for a in airports:
        city_code = a.get("city_code")
        if city_code:
            airports_per_city[city_code] = airports_per_city.get(city_code, 0) + 1
    hub_city_codes = {code for code, count in airports_per_city.items() if count >= 2}

    async with connect(settings.db_path) as db:
        code_to_id: dict[str, int] = {}

        for city in cities:
            code = city["code"]
            coords = city.get("coordinates") or {}
            cursor = await db.execute(
                """
                INSERT INTO places (kind, name_ru, name_en, iata, country, lat, lon, popularity, is_hub_candidate)
                VALUES ('city', ?, ?, ?, ?, ?, ?, 50, ?)
                ON CONFLICT(kind, iata) WHERE iata IS NOT NULL DO UPDATE SET
                    name_ru = excluded.name_ru, name_en = excluded.name_en,
                    country = excluded.country, lat = excluded.lat, lon = excluded.lon,
                    is_hub_candidate = excluded.is_hub_candidate
                """,
                (
                    city.get("name") or code,
                    (city.get("name_translations") or {}).get("en"),
                    code,
                    city["country_code"],
                    coords.get("lat"),
                    coords.get("lon"),
                    int(code in hub_city_codes),
                ),
            )
            # ON CONFLICT DO UPDATE doesn't reliably return lastrowid for the existing row on
            # all sqlite versions, so resolve the id with a follow-up read instead of trusting it.
            async with db.execute("SELECT id FROM places WHERE kind = 'city' AND iata = ?", (code,)) as cur:
                row = await cur.fetchone()
            assert row is not None
            code_to_id[code] = row[0]

        await db.commit()

        for airport in airports:
            code = airport["code"]
            city_code = airport.get("city_code")
            city_id = code_to_id.get(city_code) if city_code else None
            coords = airport.get("coordinates") or {}
            await db.execute(
                """
                INSERT INTO places (kind, name_ru, name_en, iata, city_id, country, lat, lon, popularity, is_hub_candidate)
                VALUES ('airport', ?, ?, ?, ?, ?, ?, ?, 40, ?)
                ON CONFLICT(kind, iata) WHERE iata IS NOT NULL DO UPDATE SET
                    name_ru = excluded.name_ru, name_en = excluded.name_en, city_id = excluded.city_id,
                    country = excluded.country, lat = excluded.lat, lon = excluded.lon,
                    is_hub_candidate = excluded.is_hub_candidate
                """,
                (
                    airport.get("name") or code,
                    (airport.get("name_translations") or {}).get("en"),
                    code,
                    city_id,
                    airport["country_code"],
                    coords.get("lat"),
                    coords.get("lon"),
                    int(city_code in hub_city_codes) if city_code else 0,
                ),
            )
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM places") as cur:
            total = (await cur.fetchone())[0]  # type: ignore[index]
        print(f"seeded {len(cities)} cities + {len(airports)} airports (places table now has {total} rows)")


if __name__ == "__main__":
    asyncio.run(main())
