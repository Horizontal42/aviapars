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

Also backfills `rzd_code` on city rows for the `rzd_direct` adapter, via
pass.rzd.ru's own `/suggester` autocomplete endpoint (verified live,
2026-08-17 — no bulk station dump exists for this source, unlike Yandex's
stations_list, so this is one autocomplete request per city with a pace
between requests, paced deliberately since no rate limit is published).

Correction from a live re-check (2026-08-17, second pass): the endpoint does
NOT reliably return a bare city-level node, and no candidate ever carries an
`ss` flag in practice — that first assumption was simply wrong and made the
matcher exact-string-only, which silently missed most multi-syllable or
hyphenated cities (Екатеринбург only ever appears as "ЕКАТЕРИНБУРГ-ПАССАЖИРС",
Пенза as "ПЕНЗА 1", Нижний Новгород as "НИЖНИЙ НОВГОРОД (ВСЕ ВОКЗАЛЫ)") —
233/335 cities ended up with no rzd_code at all from the original run.
Matching now takes the candidate whose name, split on the first comma, starts
with the target city name (handling a `-SUFFIX`/` SUFFIX` station qualifier),
preferring an exact head match over a suffixed one, and the highest `S`
(relevance) score as a tiebreaker. Re-verified against Moscow/SPb/Sochi (the
three previously-exact matches) to confirm this doesn't change their codes.

Second correction from a live re-check against the production DB (2026-08-19):
151/335 cities still had no rzd_code — traced to a client-side TLS trust-store
gap (`pass.rzd.ru`'s cert chains through "Russian Trusted Sub CA", not in the
standard trust store — same root cause `rzd_direct.py`'s `_ssl_context`/
`_RU_TRUSTED_CA_PEM` already fixed for search calls), not a network ban and not
a matching-quality problem on their own — every request from the unpatched
`aiohttp.ClientSession()` here was failing the handshake before ever reaching
the matcher. Once given the same trusted CA, two further gaps showed up in the
184 still-missing names: (1) hyphens in the query vs spaces in the station name
or vice versa — "Комсомольск-на-Амуре" only ever appears as "КОМСОМОЛЬСК НА
АМУРЕ П" — so hyphens/spaces are now collapsed to a single space on both sides
before comparing; (2) a trailing "(...)" qualifier Travelpayouts' own city name
sometimes carries ("Набережные Челны (Нижнекамск)", "Кировск (Апатиты)") is not
part of any real RZD station name, so it is stripped before the query is even
sent. A third, lower-confidence tier was added for adjectival station names
that extend the city name with no separator at all ("Волгодонская" for
"Волгодонск") — ranked below both the exact and separator-qualified tiers, and
gated on a minimum target length to keep short names from matching unrelated
stations by coincidence. Even with all three fixes, most of the 184 remain
unmatched — spot checks (Магадан, Норильск, Геленджик, Стрежевой, Нарьян-Мар)
found no RZD station under any spelling: real cities with no rail connection at
all, not a matcher gap. Re-verified against Moscow/SPb/Sochi/Ekaterinburg to
confirm none of this changes their existing codes.

Run: python -m aviapars.db.migrate && python data/seed_places.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aviapars.adapters.rzd_direct import _ssl_context  # noqa: E402
from aviapars.config import settings  # noqa: E402
from aviapars.db.engine import connect  # noqa: E402

CITIES_URL = "http://api.travelpayouts.com/data/ru/cities.json"
AIRPORTS_URL = "http://api.travelpayouts.com/data/ru/airports.json"
YANDEX_STATIONS_URL = "https://api.rasp.yandex-net.ru/v3.0/stations_list/"
RZD_SUGGESTER_URL = "https://pass.rzd.ru/suggester"
RZD_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; aviapars-seed/1.0)"}
RZD_PACE_S = 0.4  # gentler than the original 0.15s — a leading suspect for pass.rzd.ru IP-banning the seeder
RZD_MIN_LOOSE_MATCH_LEN = 5  # floor for the separator-less adjectival tier — short names false-match too easily


def _normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


def _normalize_for_rzd_match(name: str) -> str:
    """RZD spells the same qualifier with a hyphen in one station and a space in
    another ("Комсомольск-на-Амуре" only appears as "КОМСОМОЛЬСК НА АМУРЕ П") —
    collapsing both to a single space makes the two sides comparable.
    """
    return re.sub(r"[-\s]+", " ", _normalize(name)).strip()


def _strip_trailing_qualifier(name: str) -> str:
    """Drop a trailing "(...)" our own city name sometimes carries (a
    Travelpayouts city-merge annotation, e.g. "Набережные Челны (Нижнекамск)")
    — it is metadata about our city grouping, not part of any real RZD station
    name, so searching with it attached finds nothing.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


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

        await _backfill_rzd_codes(db)


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


async def _rzd_city_code(session: aiohttp.ClientSession, name_ru: str) -> str | None:
    query_name = _strip_trailing_qualifier(name_ru)
    params = {"stationNamePart": query_name.upper(), "lang": "ru", "lat": "0", "compactMode": "y"}
    try:
        async with session.get(
            RZD_SUGGESTER_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            candidates = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError):
        return None
    if not candidates:
        return None

    target = _normalize_for_rzd_match(query_name)
    # RZD rarely returns a bare city-name node — station names carry a
    # qualifier ("ЕКАТЕРИНБУРГ-ПАССАЖИРС", "ПЕНЗА 1", "НИЖНИЙ НОВГОРОД (ВСЕ
    # ВОКЗАЛЫ)") — so match on the head before the first comma, preferring an
    # exact head match over a suffixed one (both normalized so a hyphen on one
    # side and a space on the other still line up), then a lower-confidence
    # tier for adjectival station names glued on with no separator at all
    # ("ВОЛГОДОНСКАЯ" for "Волгодонск") — tiebreaking on RZD's own `S` score.
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for c in candidates:
        head = _normalize_for_rzd_match(c.get("n", "").split(",")[0].strip())
        if head == target:
            scored.append((2, c.get("S", 0), c))
        elif head.startswith(target + " ") or head.startswith(target + "("):
            scored.append((1, c.get("S", 0), c))
        elif len(target) >= RZD_MIN_LOOSE_MATCH_LEN and head.startswith(target):
            scored.append((0, c.get("S", 0), c))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return str(scored[0][2]["c"])


async def _backfill_rzd_codes(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT id, name_ru FROM places WHERE kind = 'city'") as cur:
        city_rows = await cur.fetchall()

    matched = 0
    connector = aiohttp.TCPConnector(ssl=_ssl_context)
    async with aiohttp.ClientSession(headers=RZD_REQUEST_HEADERS, connector=connector) as session:
        for place_id, name_ru in city_rows:
            code = await _rzd_city_code(session, name_ru)
            if code:
                await db.execute("UPDATE places SET rzd_code = ? WHERE id = ?", (code, place_id))
                matched += 1
            await asyncio.sleep(RZD_PACE_S)
    await db.commit()
    print(f"rzd_code: matched {matched}/{len(city_rows)} cities via pass.rzd.ru suggester")


if __name__ == "__main__":
    asyncio.run(main())
