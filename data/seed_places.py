"""Seeds `places` from Travelpayouts' public cities/airports data dump
(§4: "seeded from Travelpayouts + Yandex station lists, filtered to the
country list in config"). This is the same source already integrated as the
`travelpayouts` adapter, not a new one — no reverse-engineering involved,
just two public JSON files: no auth, no rate limit documented.

Idempotent: upserts on (kind, iata) via the unique index from migration 002,
so re-running refreshes data instead of duplicating rows.

If YANDEX_RASP_API_KEY is set, also backfills `yandex_code` on city rows by
matching against Yandex's stations_list settlement names — the yandex_rasp
adapter needs this and can't use it otherwise. Verified live: the stations_list
dump has no IATA field at all (checked directly against the real API,
2026-08-17) and `system=iata` only works for airports, not cities/train
stations (per Yandex's own docs) — so name matching is the only way to link
our Travelpayouts-sourced cities to Yandex's train-search codes. Matching is
exact-normalized-string only (lowercase, ё->е); cities that don't match
(renamed, ambiguous, or genuinely absent from Yandex's coverage) are simply
left without yandex_code and yandex_rasp search skips them — same graceful
degradation as any other missing-data case in this project, not a crash.

Run: python -m aviapars.db.migrate && python data/seed_places.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aviapars.config import settings  # noqa: E402
from aviapars.db.engine import connect  # noqa: E402

CITIES_URL = "http://api.travelpayouts.com/data/ru/cities.json"
AIRPORTS_URL = "http://api.travelpayouts.com/data/ru/airports.json"
YANDEX_STATIONS_URL = "https://api.rasp.yandex-net.ru/v3.0/stations_list/"


def _normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


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
            # Popularity has no real traffic signal behind it (Travelpayouts' dump doesn't
            # provide one) — airport count is the one honest proxy available, and it's exactly
            # what hub discovery (§8) needs to rank Moscow above an arbitrary same-popularity
            # multi-airport city when there's a tie at the flat default.
            popularity = 50 + airports_per_city.get(code, 0) * 10
            cursor = await db.execute(
                """
                INSERT INTO places (kind, name_ru, name_en, iata, country, lat, lon, popularity, is_hub_candidate)
                VALUES ('city', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, iata) WHERE iata IS NOT NULL DO UPDATE SET
                    name_ru = excluded.name_ru, name_en = excluded.name_en,
                    country = excluded.country, lat = excluded.lat, lon = excluded.lon,
                    popularity = excluded.popularity, is_hub_candidate = excluded.is_hub_candidate
                """,
                (
                    city.get("name") or code,
                    (city.get("name_translations") or {}).get("en"),
                    code,
                    city["country_code"],
                    coords.get("lat"),
                    coords.get("lon"),
                    popularity,
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

        if settings.yandex_rasp_api_key:
            await _backfill_yandex_codes(db)
        else:
            print("YANDEX_RASP_API_KEY not set — skipping yandex_code backfill (train search won't work yet)")


async def _backfill_yandex_codes(db: aiosqlite.Connection) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            YANDEX_STATIONS_URL,
            params={"apikey": settings.yandex_rasp_api_key, "format": "json"},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    settlement_codes: dict[str, str] = {}
    for country in data.get("countries", []):
        for region in country.get("regions", []):
            for settlement in region.get("settlements", []):
                title = settlement.get("title")
                code = (settlement.get("codes") or {}).get("yandex_code")
                if title and code:
                    settlement_codes[_normalize(title)] = code

    async with db.execute("SELECT id, name_ru FROM places WHERE kind = 'city'") as cur:
        city_rows = await cur.fetchall()

    matched = 0
    for place_id, name_ru in city_rows:
        code = settlement_codes.get(_normalize(name_ru))
        if code:
            await db.execute("UPDATE places SET yandex_code = ? WHERE id = ?", (code, place_id))
            matched += 1
    await db.commit()
    print(f"yandex_code: matched {matched}/{len(city_rows)} cities against {len(settlement_codes)} Yandex settlements")


if __name__ == "__main__":
    asyncio.run(main())
