from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

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
    build_search_result,
    is_egress_failure,
    probe_dates_total_timeout_s,
    with_dns_retry,
)

SEARCH_URL = "https://www.aeroflot.ru/sbe/flight-search/api/web/v1/flight/search"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.aeroflot.ru",
    "Referer": "https://www.aeroflot.ru/ru-ru/sb/search",
    "X-Client-Type": "web-client",
}


class AeroflotDirectAdapter:
    """Confirmation-tier direct scrape of Aeroflot Group's shared booking search.

    No JS-challenge, no cookies, no session — verified live (2026-08-17) that a
    single stateless POST with Chrome TLS impersonation (curl_cffi) returns real
    fares; plain curl gets a 503 WAF block page, so the antibot here is TLS-
    fingerprint-only, same class as `rzd_direct`/`travelpayouts`, not the
    ServicePipe-style challenge that forces `s7_direct` to keep a browser
    fallback.

    Group-shared inventory is the whole point of this adapter: Aeroflot's own
    search surfaces flights it markets (`marketing_airline_code`) but that
    Rossiya or Pobeda actually operate (`operating_airline_code`) — verified
    live that a Saint Petersburg-Sochi search returned Rossiya (FV) as the
    sole operator on every itinerary. `Leg.carrier` is set from the operating
    airline, not the marketing one, since that's the carrier the passenger
    actually flies and the one this project's roster cares about tracking
    (see ARCHITECTURE.md — Rossiya has no separate scrapeable adapter of its
    own; rossiya-airlines.ru appears to be network-blocked for non-residential
    IPs regardless of TLS fingerprint, unlike this shared endpoint).
    """

    id = "aeroflot_direct"
    display_name = "Аэрофлот / Россия / Победа"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None  # group flies international routes too
    supports_batch_dates = False
    request_timeout_s = 20
    # one POST per probe date, nothing before the loop — doubled because a blocked egress
    # makes the pool replay the whole loop through the next one
    total_timeout_s = probe_dates_total_timeout_s(
        request_timeout_s, egress_attempts=MAX_EGRESS_ATTEMPTS_PER_CALL
    )

    def __init__(
        self,
        *,
        egress_pool: Iterable[str | None] = (),
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 1800,
    ) -> None:
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=15.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._egress = EgressPool(egress_pool)

    def _deep_link(self, q: SearchQuery, depart_date: date) -> str:
        """Same query shape as a real user's search — captured verbatim from
        the site's own page URL in a live browser session (www.aeroflot.ru.har)."""
        params = {
            "adults": q.pax,
            "infants": 0,
            "children": 0,
            "childrenfrgn": 0,
            "childrenaward": 0,
            "cabin": "economy",
            "routes": f"{q.origin.iata}.{depart_date.strftime('%Y%m%d')}.{q.dest.iata}",
        }
        return f"https://www.aeroflot.ru/ru-ru/sb/search?{urlencode(params)}"

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.PLANE:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.iata or not q.dest.iata or not q.dates:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing iata/date")

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

        return build_search_result(offers, blocked=blocked, partial=partial)

    async def _search_day(self, session: Any, q: SearchQuery, depart_date: date) -> tuple[list[Offer], str]:
        """Returns (offers, outcome) where outcome is "ok" (incl. legitimately empty),
        "blocked" (503 or a connection that never completed), or "error" (parse/other
        failure) — mirrors rzd_direct's per-day result so one bad date in the window
        doesn't sink the whole cycle."""
        body = {
            "routes": [
                {"origin": q.origin.iata, "destination": q.dest.iata, "departure_date": depart_date.isoformat()}
            ],
            "country": "RU",
            "lang": "ru",
            "adults": q.pax,
            "cabin": "economy",
            "children": 0,
            "childrenfrgn": 0,
            "infants": 0,
            "award": False,
            "coupons": [],
            "vouchers": [],
            "days": 1,
        }

        try:
            resp = await with_dns_retry(lambda: session.post(SEARCH_URL, headers=_HEADERS, json=body))
        except Exception as exc:
            return [], "blocked" if is_egress_failure(exc) else "error"

        if resp.status_code == 503:
            return [], "blocked"
        if resp.status_code != 200:
            return [], "error"
        try:
            payload = resp.json()
        except ValueError:
            return [], "error"

        return self._parse_offers(payload, q, depart_date), "ok"

    def _parse_offers(self, payload: dict[str, Any], q: SearchQuery, depart_date: date) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)
        groups = (payload.get("data") or {}).get("route_itineraries") or []
        itineraries = groups[0] if groups else []

        for itinerary in itineraries:
            legs_raw = itinerary.get("legs") or []
            offer_options = itinerary.get("offers") or []
            if not legs_raw or not offer_options:
                continue

            cheapest = min(offer_options, key=lambda o: Decimal(o["price_total_amount"]))

            segments = [seg for leg in legs_raw for seg in (leg.get("segments") or [])]
            legs = [self._parse_segment(seg, q, first=i == 0, last=i == len(segments) - 1) for i, seg in enumerate(segments)]
            if not legs:
                continue

            price = Decimal(cheapest["price_total_amount"])
            itinerary_hash = hashlib.sha256(
                f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                f"{legs[0].number}|{cheapest.get('brand_code')}".encode()
            ).hexdigest()[:16]

            offers.append(
                Offer(
                    legs=tuple(legs),
                    price_minor=round(price * 100),
                    currency=cheapest.get("currency_code", q.currency).upper(),
                    deep_link=self._deep_link(q, depart_date),
                    source_id=self.id,
                    tier=Tier.CONFIRMATION,
                    observed_at=now,
                    source_cached_at=now,
                    expires_at=None,
                    confidence=1.0,
                    itinerary_hash=itinerary_hash,
                    duration_minutes=_total_duration_minutes(legs),
                )
            )
        return offers

    def _parse_segment(self, seg: dict[str, Any], q: SearchQuery, *, first: bool, last: bool) -> Leg:
        dep_local = datetime.fromisoformat(seg["departure"])
        dep_utc = _as_utc(datetime.fromisoformat(seg["departure_utc"]))
        arr_utc = None
        if seg.get("arrival_utc"):
            arr_utc = _as_utc(datetime.fromisoformat(seg["arrival_utc"]))
        arr_local = datetime.fromisoformat(seg["arrival"]) if seg.get("arrival") else None
        offset_min = seg.get("departure_offset")
        tz = _format_offset(offset_min) if offset_min is not None else None

        return Leg(
            mode=Mode.PLANE,
            from_place=q.origin if first else _segment_place(seg.get("origin")),
            to_place=q.dest if last else _segment_place(seg.get("destination")),
            depart_utc=dep_utc,
            depart_local=dep_local,
            carrier=seg.get("operating_airline_name") or seg.get("marketing_airline_name"),
            number=seg.get("operating_flight_number") or seg.get("marketing_flight_number"),
            arrive_utc=arr_utc,
            arrive_local=arr_local,
            tz=tz,
            time_precision="exact",
        )

    async def health_check(self) -> HealthStatus:
        probe = SearchQuery(
            origin=PlaceRef(place_id=0, iata="LED", yandex_code=None, rzd_code=None, city_id=None),
            dest=PlaceRef(place_id=0, iata="SVO", yandex_code=None, rzd_code=None, city_id=None),
            dates=(date.today() + timedelta(days=14),),
            pax=1,
            mode=Mode.PLANE,
        )
        try:
            result = await self.search(probe)
            return HealthStatus(ok=result.status in (ResultStatus.OK, ResultStatus.EMPTY))
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))


def _segment_place(raw: dict[str, Any] | None) -> PlaceRef:
    """The airport a *connection* passes through, from the segment's own payload.

    Every segment used to be labelled with the query's own origin and destination, so a
    LED→SVO→AER itinerary rendered as two identical "LED → AER" legs — harmless-looking
    until the card started naming the transfer point, at which point it confidently
    announced a layover in Сочи that happens in Москва.

    Only ever used for the inner endpoints. The outer two keep the query's PlaceRef
    because that one carries the real `place_id`, which grouping and the cooldown key
    are built from (domain/grouping._shape) — a synthetic id there would stop an
    Aeroflot offer from ever matching another source's for the same route.
    """
    raw = raw or {}
    return PlaceRef(
        place_id=0,  # not a row in `places`: a hub airport is not necessarily tracked there
        iata=raw.get("airport_code"),
        yandex_code=None,
        rzd_code=None,
        city_id=None,
        name_ru=raw.get("city_name") or raw.get("airport_name"),
    )


def _total_duration_minutes(legs: list[Leg]) -> int | None:
    """Gate-to-gate, layovers included — first departure to last arrival.

    Every other adapter in the roster fills `duration_minutes` itself; this one used to
    leave it None even though it has both timestamps, so its deals rendered as
    «⏱ В пути: неизвестно» and had nothing for cross-source grouping to corroborate
    against (see domain/grouping.py). Summing the segments' own `duration_minutes`
    would have silently dropped the connection wait on a multi-segment itinerary.
    """
    arrive = legs[-1].arrive_utc
    if arrive is None:
        return None
    return int((arrive - legs[0].depart_utc).total_seconds() // 60)


def _format_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h:02d}:{m:02d}"


def _as_utc(parsed: datetime) -> datetime:
    """The recorded fixture's `*_utc` fields are naive today, but `.replace(tzinfo=utc)`
    would silently discard a real offset if Aeroflot ever starts sending one — `.astimezone`
    only when the value is actually aware, otherwise treat naive as already-UTC per the
    field's own name."""
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
