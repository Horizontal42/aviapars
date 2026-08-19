from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

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
    PlaceRef,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    is_egress_failure,
    probe_dates_total_timeout_s,
    with_dns_retry,
)

OFFERS_URL = "https://offers-api.tutu.ru/railway/offers"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.tutu.ru",
    "Referer": "https://www.tutu.ru/",
}
# rzd_code doubles as Tutu's station code (§ class docstring) — health_check
# only needs origin/dest.rzd_code, so a minimal PlaceRef is enough here.
_PLACE_MOW = PlaceRef(place_id=0, iata=None, yandex_code=None, rzd_code="2000000", city_id=None)
_PLACE_LED = PlaceRef(place_id=0, iata=None, yandex_code=None, rzd_code="2004000", city_id=None)


class TutuRailDirectAdapter:
    """Confirmation-tier train prices from Tutu's own internal offers API
    (offers-api.tutu.ru/railway/offers) — the same JSON endpoint tutu.ru's own
    frontend calls, not the documented-as-stale Tutu partner API rejected in
    the original plan (plans/squishy-scribbling-dawn.md §7).

    Exists as a second live train price source alongside rzd_direct, and the
    de facto primary one while rzd_direct's VDS IP ban is unresolved (see
    README "Известное ограничение" / ARCHITECTURE.md). No adapter deletion —
    if RZD's ban ever lifts, both run side by side for cross-validation.

    Reuses `places.rzd_code` directly — Tutu's `departureStationCode` /
    `arrivalStationCode` params are literally RZD's own Express-3 station
    codes (confirmed live: rzd_direct's own health_check probes Moscow->SPb
    with the identical codes "2000000"/"2004000" that Tutu's request body
    uses). No new place-id namespace, no new DB column.

    Auth: none. `searchId`/`sessionId`/`pageId` are freely-generated
    identifiers, not server-issued tokens — verified live with fresh random
    UUIDs and no cookies, 200 OK with full real fares.

    Response shape: the endpoint streams 1+ JSON objects in one array as the
    search progresses across train operators (verified live: two snapshots
    for one query, overlapping but not identical offer sets — some offers
    present in an earlier snapshot dropped out of the later one, presumably
    sold out mid-search). `_merge_chunks` unions offers and dictionary
    entries across every chunk so a transient offer isn't silently lost.

    Unlike rzd_direct (single leg, Moscow-time-assumed), a Tutu offer's route
    can carry multiple segments (interchange trains) and every segment's
    departure/arrival is a real ISO-8601 timestamp with its own UTC offset —
    no per-station timezone guess needed.

    `supports_batch_dates = False` means the combiner hands `search()` a
    whole cycle's worth of dates at once (`flex_probe_dates_per_cycle`,
    default 7 — see domain/dates.py's rotating-cursor plan, not all 30 days
    of a flexible window in one go). `search()` must loop over every date in
    `q.dates` itself, same as rzd_direct/yandex_rasp/s7_direct — a query for
    N dates costs N sequential POSTs in one `search()` call, one date's
    failure doesn't drop the others.
    """

    id = "tutu_rail_direct"
    display_name = "Туту.ру (поезда)"
    modes = frozenset({Mode.TRAIN})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = frozenset({"RU"})
    supports_batch_dates = False
    request_timeout_s = 20
    # one POST per probe date (station codes come from places.rzd_code, no lookup needed),
    # doubled because a blocked egress makes the pool replay the whole loop through the next
    total_timeout_s = probe_dates_total_timeout_s(
        request_timeout_s, egress_attempts=MAX_EGRESS_ATTEMPTS_PER_CALL
    )

    def __init__(
        self,
        *,
        egress_pool: Iterable[str | None] = (),
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 2700,
    ) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=15.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._egress = EgressPool(egress_pool)

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.TRAIN:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.rzd_code or not q.dest.rzd_code or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing rzd_code/date")

        return await self._egress.run(lambda proxy_url: self._search_via(q, proxy_url))

    async def _search_via(self, q: SearchQuery, proxy_url: str | None) -> SearchResult:
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
            for depart_date in q.dates:
                day_offers, outcome = await self._search_day(session, q, depart_date)
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
        self, session: Any, q: SearchQuery, depart_date: date
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
            "routes": [
                {
                    "departureStationCode": q.origin.rzd_code,
                    "arrivalStationCode": q.dest.rzd_code,
                    "departureDate": depart_date.isoformat(),
                }
            ],
            "analyticsPassengers": {"adultCount": q.pax, "children": []},
            "searchId": str(uuid.uuid4()),
            "sessionId": str(uuid.uuid4()),
            "pageId": uuid.uuid4().hex[:11],
            "flags": [
                {"name": "interchangeOffersStrategy", "value": "ON_EMPTY_DIRECT_SEARCH"},
                {"name": "possibleOffersEnabled", "value": "ENABLED"},
            ],
            "userData": {},
            "source": "trainOffers",
        }

        try:
            resp = await with_dns_retry(lambda: session.post(OFFERS_URL, headers=_HEADERS, json=body))
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

        return self._parse_offers(payload, q, depart_date), "ok"

    def _merge_chunks(self, payload: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        merged_offers: dict[str, Any] = {}
        routes: dict[str, Any] = {}
        segments: dict[str, Any] = {}
        carriers: dict[str, Any] = {}
        voyages: dict[str, Any] = {}
        for chunk in payload:
            common = (chunk.get("dictionary") or {}).get("common") or {}
            routes.update(common.get("routes") or {})
            segments.update(common.get("segments") or {})
            carriers.update(common.get("carriers") or {})
            train_dict = (chunk.get("dictionary") or {}).get("train") or {}
            voyages.update(train_dict.get("voyages") or {})
            merged_offers.update((chunk.get("offers") or {}).get("actual") or {})
        return merged_offers, {"routes": routes, "segments": segments, "carriers": carriers, "voyages": voyages}

    def _parse_offers(self, payload: list[dict[str, Any]], q: SearchQuery, depart_date: date) -> list[Offer]:
        offers_dict, lookups = self._merge_chunks(payload)
        routes, segments, carriers, voyages = (
            lookups["routes"],
            lookups["segments"],
            lookups["carriers"],
            lookups["voyages"],
        )
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

                carrier_id = ((seg.get("carriers") or [{}])[0]).get("id")
                carrier_name = (carriers.get(carrier_id) or {}).get("name") if carrier_id else None
                voyage = voyages.get(seg.get("voyageNumber") or "") or {}
                number = voyage.get("numberForPassengers") or voyage.get("number")

                legs.append(
                    Leg(
                        mode=Mode.TRAIN,
                        from_place=q.origin,
                        to_place=q.dest,
                        depart_utc=dep.astimezone(timezone.utc),
                        depart_local=dep,
                        carrier=carrier_name,
                        number=number,
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
                f"{self.id}|{q.origin.rzd_code}|{q.dest.rzd_code}|{depart_date.isoformat()}|{offer_id}".encode()
            ).hexdigest()[:16]

            result.append(
                Offer(
                    legs=tuple(legs),
                    # `.get(k, default)` only covers a missing key — Tutu has sent an
                    # explicit `null` for either field before; `or` catches that too
                    # (and a `fraction: 0` would otherwise ZeroDivisionError).
                    price_minor=round(price["amount"] / (price.get("fraction") or 100) * 100),
                    currency=price.get("currencyCode") or "RUB",
                    # not prefilled: the real page URL uses a city-name slug in the path
                    # (www.tutu.ru.har: /poezda/Moskva/Sankt-Peterburg/?date=...), and this
                    # adapter only has rzd_code, no slug lookup — fabricating one risks a
                    # broken link, so this points at the plain search page instead.
                    deep_link="https://www.tutu.ru/poezda/",
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
            result = await self.search(
                SearchQuery(
                    origin=_PLACE_MOW,
                    dest=_PLACE_LED,
                    dates=(date.today(),),
                    pax=1,
                    mode=Mode.TRAIN,
                )
            )
            return HealthStatus(ok=result.status in (ResultStatus.OK, ResultStatus.EMPTY))
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
