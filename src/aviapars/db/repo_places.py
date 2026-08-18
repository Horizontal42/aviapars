from __future__ import annotations

import aiosqlite

from aviapars.db.engine import normalize_place_query
from aviapars.db.models import Place
from aviapars.domain.geo import Point, detour_km, haversine_km


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
        (§10) — no external API call. Match is against `normalize(name)`
        (casefold + strip spaces/hyphens/punctuation, see db/engine.py) so
        "Санкт-Петербург", "санкт петербург" and "saint-petersburg" all hit —
        plain SQL LIKE only case-folds ASCII, never Cyrillic. Ranked by
        popularity so a well-known city outranks an obscure station with a
        similar name.
        """
        like = f"%{normalize_place_query(query)}%"
        async with self._db.execute(
            f"""
            SELECT {_COLUMNS} FROM places
            WHERE normalize(name_ru) LIKE ? OR normalize(name_en) LIKE ? OR iata = ?
            ORDER BY popularity DESC
            LIMIT ?
            """,
            (like, like, query.strip().upper(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_place(row) for row in rows]

    async def hub_candidates_near(self, origin: Place, dest: Place, *, limit: int = 3) -> list[Place]:
        """Dynamic hub discovery (§8) — no hardcoded city list.

        SQL only *filters* (same country as an endpoint, not degenerate);
        the actual pick is `rank_hub_candidates` below, which reads the
        coordinates the schema has carried since 001 and nothing looked at.

        Excludes by city_id, not place id: origin/dest are place rows (one
        airport, one station), but a hub candidate is degenerate the moment
        it's in the *same city* as either endpoint, not just the same exact
        row — e.g. proposing origin's own train station as a "hub" for a
        route departing from origin's airport. Uses `IS NOT` rather than
        `NOT IN` for the city_id comparison: with plain `NOT IN`, a NULL
        origin/dest city_id (unmapped place) poisons SQL's three-valued
        logic and silently excludes every candidate row, not just the ones
        that actually share a city — `IS NOT` compares NULL safely instead.

        The SQL pool is capped by popularity purely to bound the row count
        (`_CANDIDATE_POOL` is far above how many hub candidates any one
        country realistically has — 17 in the live RU seed), not to pre-rank:
        truncating by popularity here would smuggle the old bias back in.
        """
        async with self._db.execute(
            f"""
            SELECT {_COLUMNS} FROM places
            WHERE is_hub_candidate = 1
              AND country IN (?, ?)
              AND id NOT IN (?, ?)
              AND (city_id IS NULL OR (city_id IS NOT ? AND city_id IS NOT ?))
            ORDER BY popularity DESC
            LIMIT ?
            """,
            (
                origin.country,
                dest.country,
                origin.id,
                dest.id,
                origin.city_id,
                dest.city_id,
                _CANDIDATE_POOL,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        return rank_hub_candidates(origin, dest, [_row_to_place(row) for row in rows], limit=limit)


_CANDIDATE_POOL = 100

# How far off the straight line a hub may sit before it stops being a plausible
# routing at all: the larger of a flat allowance (short hops still deserve *some*
# slack — a 350 km trip has no useful hub within 350 km of detour) and the direct
# distance itself (a 100% excursion is the outer edge of what a hub with real
# inventory can still win on price).
MIN_DETOUR_ALLOWANCE_KM = 500.0
MAX_DETOUR_RATIO = 1.0
# Exchange rate between the two signals once a candidate is inside the bound:
# 10 km of detour is worth one popularity point. Calibrated against the live
# seed's spread (regional cities 50-60, Moscow 90): Moscow keeps winning where
# it genuinely sits on the path (Penza→Kaliningrad, ~0 detour), while on
# Penza→Sochi its ~810 km excursion costs 81 points and a regional hub like
# Samara (~500 km) lands close enough behind to be tried alongside it, which is
# the whole point — before this, geography contributed nothing at all.
DETOUR_KM_PER_POPULARITY_POINT = 10.0


def rank_hub_candidates(origin: Place, dest: Place, candidates: list[Place], *, limit: int) -> list[Place]:
    """Pick hubs by where they actually are, not by popularity alone (§8).

    The bug this replaces: ranking `is_hub_candidate` rows by `popularity DESC`
    with no geography meant Moscow (90 in the live seed, next tier 70) plus one
    arbitrary filler — a tiny Siberian airport thousands of km off the path —
    were offered for *every* route in the country. Not hardcoded, but
    indistinguishable from it in effect.

    Candidates with no coordinates can't be scored and must not win by default
    (a missing lat/lon is not "on the route"), so they are ranked strictly
    behind every geo-scored candidate, by popularity, as filler. If an
    *endpoint* has no coordinates nothing can be scored at all and this
    degrades to the old popularity order — the honest fallback, since the
    input for anything better simply isn't there.
    """
    endpoints = _point(origin), _point(dest)
    if endpoints[0] is None or endpoints[1] is None:
        return sorted(candidates, key=lambda p: -p.popularity)[:limit]
    origin_pt, dest_pt = endpoints[0], endpoints[1]

    direct = haversine_km(origin_pt, dest_pt)
    allowance = max(MIN_DETOUR_ALLOWANCE_KM, direct * MAX_DETOUR_RATIO)

    scored: list[tuple[float, Place]] = []
    coordless: list[Place] = []
    for place in candidates:
        point = _point(place)
        if point is None:
            coordless.append(place)
            continue
        detour = detour_km(origin_pt, point, dest_pt)
        if detour > allowance:
            continue
        scored.append((place.popularity - detour / DETOUR_KM_PER_POPULARITY_POINT, place))

    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    ranked = [place for _, place in scored]
    ranked.extend(sorted(coordless, key=lambda p: (-p.popularity, p.id)))
    return ranked[:limit]


def _point(place: Place) -> Point | None:
    if place.lat is None or place.lon is None:
        return None
    return place.lat, place.lon
