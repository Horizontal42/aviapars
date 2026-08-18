from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

from aviapars.adapters._tutu_common import select_price_variant
from aviapars.adapters.base import (
    HealthStatus,
    Leg,
    Mode,
    Offer,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    probe_dates_total_timeout_s,
)

SUGGEST_URL = "https://suggester-avia.tutu.ru/api/location_suggest/v2"
OFFERS_URL = "https://offers-api.tutu.ru/avia/offers/v2"
_SUGGEST_HEADERS = {"Origin": "https://avia.tutu.ru", "Referer": "https://avia.tutu.ru/"}
_OFFERS_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "text/plain;charset=UTF-8",
    "Origin": "https://avia.tutu.ru",
    "Referer": "https://avia.tutu.ru/",
}


def _deep_link(q: SearchQuery, origin_id: str, dest_id: str, depart_date: date) -> str:
    """`route[0]=<cityId>-<DDMMYYYY>-<cityId>` is the real query shape captured from a
    live browser session (avia.tutu.ru.har's page URL); city name path slugs
    (e.g. /f/Moskva/Sankt-peterburg/) turned out to be cosmetic — the bare `/f/` path
    plus this query still returns 200 and the SPA reads the route from the query."""
    params = {"class": "Y", "travelers": q.pax, "route[0]": f"{origin_id}-{depart_date.strftime('%d%m%Y')}-{dest_id}"}
    return f"https://avia.tutu.ru/f/?{urlencode(params)}"


class TutuAviaDirectAdapter:
    """Confirmation-tier plane prices from Tutu's own internal offers API
    (offers-api.tutu.ru/avia/offers/v2) — the avia sibling of tutu_rail_direct,
    same platform, same response shape (dictionary + offers.actual keyed by
    offer id, price.type == "exact" vs "absent" waitlist placeholders).

    Exists because tutu.ru's search is a real OTA aggregating many carriers
    behind one endpoint — a hedge against gaps in the airline-by-airline
    roster (Red Wings/Smartavia/Ural/Yakutia not built yet, Utair blocked)
    and a cross-check for the ones that are built.

    Auth: none — verified live with fresh random searchId/sessionId/pageId
    and no cookies, 200 OK with real fares.

    City resolution: unlike rail (which reuses `places.rzd_code` directly),
    avia routes need Tutu's own numeric city id. `suggester-avia.tutu.ru`
    resolves IATA -> city id with a plain text query (verified live:
    `?name=SVX` returns `city.id` for the matching `city.code == "SVX"`
    suggestion) — same dynamic-resolve-and-cache pattern as
    `s7_direct._resolve_location_id`, not a new DB column.

    `supports_batch_dates = False` means the combiner hands `search()` a
    whole cycle's worth of dates at once (`flex_probe_dates_per_cycle`,
    default 7 - see domain/dates.py's rotating-cursor plan, not all 30 days
    of a flexible window in one go). `search()` loops over every date in
    `q.dates`, resolving city ids once up front (cached, so a 7-date query
    only pays for 2 suggester GETs, not 14) and then one offers POST per
    date - one date's failure doesn't drop the others.
    """

    id = "tutu_avia_direct"
    display_name = "Туту.ру (самолёты)"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = frozenset({"RU"})
    supports_batch_dates = False
    request_timeout_s = 20
    # one offers POST per probe date, preceded by 2 suggester GETs (origin + dest city id,
    # memoised after the first call — the ceiling has to cover the cold one)
    total_timeout_s = probe_dates_total_timeout_s(request_timeout_s, setup_s=2 * request_timeout_s)

    def __init__(self, *, rate_limit: RateLimitPolicy | None = None, cache_ttl_s: int = 1800) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=12.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._city_id_cache: dict[str, str] = {}

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        offers: list[Offer] = []
        blocked = False
        partial = False
        async with AsyncSession(impersonate="chrome", timeout=self.request_timeout_s) as session:
            origin_id = await self._resolve_city_id(session, q.origin.iata)
            dest_id = await self._resolve_city_id(session, q.dest.iata)
            if origin_id is None or dest_id is None:
                return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="city not found")

            for depart_date in q.dates:
                day_offers, outcome = await self._search_day(session, q, origin_id, dest_id, depart_date)
                if outcome == "blocked":
                    blocked = True
                    continue
                if outcome == "error":
                    partial = True
                    continue
                offers.extend(day_offers)

        if offers:
            status = ResultStatus.OK
        elif blocked:
            status = ResultStatus.BLOCKED
        elif partial:
            status = ResultStatus.UPSTREAM_ERROR
        else:
            status = ResultStatus.EMPTY
        return SearchResult(status=status, offers=offers, partial=partial)

    async def _search_day(
        self, session: Any, q: SearchQuery, origin_id: str, dest_id: str, depart_date: date
    ) -> tuple[list[Offer], str]:
        """Returns (offers, outcome) where outcome is "ok" (incl. legitimately empty),
        "blocked" (403), or "error" (network/parse failure) — mirrors rzd_direct's
        per-day result so one bad date in the window doesn't sink the whole cycle."""
        body = {
            "passengers": {"child": 0, "infant": 0, "full": q.pax},
            "serviceClass": "Y",
            "routes": [
                {"departureCityId": origin_id, "arrivalCityId": dest_id, "departureDate": depart_date.isoformat()}
            ],
            "searchId": str(uuid.uuid4()),
            "sessionId": str(uuid.uuid4()),
            "pageId": uuid.uuid4().hex[:11],
            "userData": {},
            "source": "offers",
            "dynamicFilters": {
                "optionFilters": [],
                "rangeFilters": [],
                "buttonFilters": [],
                "shortcuts": [],
                "switchFilters": [],
            },
            "offset": 0,
            "isNewSearch": True,
        }
        try:
            resp = await session.post(OFFERS_URL, headers=_OFFERS_HEADERS, data=json.dumps(body))
        except Exception:
            return [], "error"

        if resp.status_code == 403:
            return [], "blocked"
        if resp.status_code != 200:
            return [], "error"
        try:
            payload = resp.json()
        except ValueError:
            return [], "error"
        if not isinstance(payload, list) or not payload:
            return [], "ok"  # legitimate "nothing found" — not a failure

        deep_link = _deep_link(q, origin_id, dest_id, depart_date)
        return self._parse_offers(payload, q, depart_date, deep_link), "ok"

    async def _resolve_city_id(self, session: Any, iata: str) -> str | None:
        cached = self._city_id_cache.get(iata)
        if cached is not None:
            return cached
        try:
            resp = await session.get(SUGGEST_URL, headers=_SUGGEST_HEADERS, params={"name": iata})
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        for suggestion in payload.get("suggestions") or []:
            city = suggestion.get("city") or {}
            if city.get("code") == iata:
                city_id: str = city["id"]
                self._city_id_cache[iata] = city_id
                return city_id
        return None

    def _merge_chunks(self, payload: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        merged_offers: dict[str, Any] = {}
        routes: dict[str, Any] = {}
        segments: dict[str, Any] = {}
        carriers: dict[str, Any] = {}
        for chunk in payload:
            common = (chunk.get("dictionary") or {}).get("common") or {}
            routes.update(common.get("routes") or {})
            segments.update(common.get("segments") or {})
            carriers.update(common.get("carriers") or {})
            merged_offers.update((chunk.get("offers") or {}).get("actual") or {})
        return merged_offers, {"routes": routes, "segments": segments, "carriers": carriers}

    def _parse_offers(
        self, payload: list[dict[str, Any]], q: SearchQuery, depart_date: date, deep_link: str | None = None
    ) -> list[Offer]:
        offers_dict, lookups = self._merge_chunks(payload)
        routes, segments, carriers = lookups["routes"], lookups["segments"], lookups["carriers"]
        now = datetime.now(timezone.utc)
        result: list[Offer] = []

        for offer_id, offer in offers_dict.items():
            route_ids = offer.get("routeIds") or []
            if not route_ids:
                continue
            route = routes.get(route_ids[0])
            if route is None:
                continue
            seg_ids = route.get("segmentIds") or []
            if not seg_ids:
                continue

            legs: list[Leg] = []
            for seg_id in seg_ids:
                seg = segments.get(seg_id)
                if seg is None:
                    legs = []
                    break
                try:
                    dep = datetime.fromisoformat(seg["departureDateTime"])
                    arr = datetime.fromisoformat(seg["arrivalDateTime"]) if seg.get("arrivalDateTime") else None
                except (KeyError, ValueError):
                    legs = []
                    break

                seg_carriers = seg.get("carriers") or []
                operating = next((c for c in seg_carriers if c.get("type") == "operating"), None)
                carrier_id = (operating or (seg_carriers[0] if seg_carriers else {})).get("id")
                carrier_name = (carriers.get(carrier_id) or {}).get("name") if carrier_id else None

                legs.append(
                    Leg(
                        mode=Mode.PLANE,
                        from_place=q.origin,
                        to_place=q.dest,
                        depart_utc=dep.astimezone(timezone.utc),
                        depart_local=dep,
                        carrier=carrier_name,
                        number=seg.get("voyageNumber"),
                        arrive_utc=arr.astimezone(timezone.utc) if arr else None,
                        tz=dep.tzname(),
                        time_precision="exact",
                    )
                )
            if not legs:
                continue

            selected = select_price_variant(offer.get("offerVariants") or [])
            if selected is None:
                continue
            chosen, confidence, tier = selected
            price = chosen["price"]["value"]

            duration = None
            if legs[-1].arrive_utc:
                duration = int((legs[-1].arrive_utc - legs[0].depart_utc).total_seconds() // 60)

            itinerary_hash = hashlib.sha256(
                f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|{offer_id}".encode()
            ).hexdigest()[:16]

            result.append(
                Offer(
                    legs=tuple(legs),
                    # `.get(k, default)` only covers a missing key — Tutu has sent an
                    # explicit `null` for either field before; `or` catches that too
                    # (and a `fraction: 0` would otherwise ZeroDivisionError).
                    price_minor=round(price["amount"] / (price.get("fraction") or 100) * 100),
                    currency=price.get("currencyCode") or "RUB",
                    deep_link=deep_link,
                    source_id=self.id,
                    tier=tier,
                    observed_at=now,
                    source_cached_at=now,
                    expires_at=None,
                    confidence=confidence,
                    itinerary_hash=itinerary_hash,
                    duration_minutes=duration,
                )
            )
        return result

    async def health_check(self) -> HealthStatus:
        try:
            async with AsyncSession(impersonate="chrome", timeout=self.request_timeout_s) as session:
                city_id = await self._resolve_city_id(session, "MOW")
            return HealthStatus(ok=city_id is not None)
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
