from __future__ import annotations

import hashlib
import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

from aviapars.adapters._tutu_common import select_price_variant
from aviapars.adapters.base import (
    CURL_FORCE_IPV4,
    MAX_EGRESS_ATTEMPTS_PER_CALL,
    EgressPool,
    HealthStatus,
    Leg,
    Mode,
    Offer,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    build_search_result,
    is_egress_failure,
    probe_dates_total_timeout_s,
    with_dns_retry,
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

    2026-08: endpoint started returning an instant empty-body 500 on every request.
    Root cause (found by reverse-engineering avia.tutu.ru's current Next.js results
    app bundles, then bisecting the POST body live): Tutu tightened `routes[].
    departureCityId`/`arrivalCityId` to require JSON integers - the adapter was
    sending the suggester's city id as a string. URL, headers, auth, and the whole
    response shape are unchanged; only the two ids need `int(...)` at the POST
    body's construction site (see `_search_day`).

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
    # memoised after the first call — the ceiling has to cover the cold one), all of it
    # doubled because a blocked egress makes the pool replay the whole thing through the next
    total_timeout_s = probe_dates_total_timeout_s(
        request_timeout_s, setup_s=2 * request_timeout_s, egress_attempts=MAX_EGRESS_ATTEMPTS_PER_CALL
    )

    def __init__(
        self,
        *,
        egress_pool: Iterable[str | None] = (),
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 1800,
    ) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=12.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._city_id_cache: dict[str, str] = {}
        self._egress = EgressPool(egress_pool)

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

        return await self._egress.run(lambda proxy_url: self._search_via(q, proxy_url))

    async def _search_via(self, q: SearchQuery, proxy_url: str | None) -> SearchResult:
        # search() already rejected the query if either iata was missing.
        assert q.origin.iata is not None and q.dest.iata is not None
        session_kwargs: dict[str, Any] = {
            "impersonate": "chrome",
            "timeout": self.request_timeout_s,
            "curl_options": CURL_FORCE_IPV4,
        }
        if proxy_url:
            session_kwargs["proxy"] = proxy_url

        offers: list[Offer] = []
        blocked = False
        partial = False
        async with AsyncSession(**session_kwargs) as session:
            origin_id, origin_outcome = await self._resolve_city_id(session, q.origin.iata)
            dest_id, dest_outcome = await self._resolve_city_id(session, q.dest.iata)
            if origin_id is None or dest_id is None:
                # A suggester call that never completed is the egress failing, not Tutu
                # saying the city doesn't exist — reporting EMPTY there would tell the pool
                # this egress works and leave the adapter pinned to a dead one.
                if "blocked" in (origin_outcome, dest_outcome):
                    return SearchResult(status=ResultStatus.BLOCKED, offers=[], partial=True)
                if "error" in (origin_outcome, dest_outcome):
                    return SearchResult(status=ResultStatus.UPSTREAM_ERROR, offers=[], partial=True)
                return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="city not found")

            tasks = [self._search_day(session, q, origin_id, dest_id, depart_date) for depart_date in q.dates]
            results = await asyncio.gather(*tasks)
            for day_offers, outcome in results:
                if outcome == "blocked":
                    blocked = True
                    continue
                if outcome == "error":
                    partial = True
                    continue
                offers.extend(day_offers)

        return build_search_result(offers, blocked=blocked, partial=partial)

    async def _search_day(
        self, session: Any, q: SearchQuery, origin_id: str, dest_id: str, depart_date: date
    ) -> tuple[list[Offer], str]:
        """Returns (offers, outcome) where outcome is "ok" (incl. legitimately empty),
        "blocked", or "error" (parse/other failure) — mirrors rzd_direct's per-day
        result so one bad date in the window doesn't sink the whole cycle.

        "blocked" here is mostly *not* a 403: tutu.ru's network-level ban drops the
        connection silently with no HTTP response at all, so `is_egress_failure`
        (which reads that off curl's exception type) is what actually feeds the
        egress pool for this adapter. The 403 branch stays for the ordinary WAF
        answer the endpoint can also give."""
        body = {
            "passengers": {"child": 0, "infant": 0, "full": q.pax},
            "serviceClass": "Y",
            "routes": [
                {
                    # Tutu's backend now rejects (empty-body 500) city ids sent as JSON
                    # strings - verified live: identical body with these two fields as ints
                    # instead of strings is the only change needed to get a 200. `origin_id`/
                    # `dest_id` stay strings everywhere else (cache key, `_deep_link`'s query
                    # string) - only this POST body needs the numeric form.
                    "departureCityId": int(origin_id),
                    "arrivalCityId": int(dest_id),
                    "departureDate": depart_date.isoformat(),
                }
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
            resp = await with_dns_retry(
                lambda: session.post(OFFERS_URL, headers=_OFFERS_HEADERS, data=json.dumps(body))
            )
        except Exception as exc:
            return [], "blocked" if is_egress_failure(exc) else "error"

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

    async def _resolve_city_id(self, session: Any, iata: str) -> tuple[str | None, str]:
        """Returns (city_id, outcome) with the same "ok"/"blocked"/"error" vocabulary as
        `_search_day`. The outcome is not decoration: the suggester lives on the same
        tutu.ru infrastructure as the offers endpoint and gets banned with it, so a
        failure here has to be distinguishable from a genuine "no such city" — those
        two used to collapse into the same bare `None`."""
        cached = self._city_id_cache.get(iata)
        if cached is not None:
            return cached, "ok"
        try:
            resp = await with_dns_retry(
                lambda: session.get(SUGGEST_URL, headers=_SUGGEST_HEADERS, params={"name": iata})
            )
        except Exception as exc:
            return None, "blocked" if is_egress_failure(exc) else "error"
        if resp.status_code == 403:
            return None, "blocked"
        if resp.status_code != 200:
            return None, "error"
        try:
            payload = resp.json()
        except ValueError:
            return None, "error"
        for suggestion in payload.get("suggestions") or []:
            city = suggestion.get("city") or {}
            if city.get("code") == iata:
                city_id: str = city["id"]
                self._city_id_cache[iata] = city_id
                return city_id, "ok"
        return None, "ok"

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
                        arrive_local=arr,
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
            async with AsyncSession(
                impersonate="chrome", timeout=self.request_timeout_s, curl_options=CURL_FORCE_IPV4
            ) as session:
                city_id, _ = await self._resolve_city_id(session, "MOW")
            return HealthStatus(ok=city_id is not None)
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
