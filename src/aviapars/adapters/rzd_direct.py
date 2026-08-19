from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp_socks import ProxyConnector

from aviapars.adapters.base import (
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
    probe_dates_total_timeout_s,
)

BASE_URL = "https://pass.rzd.ru/timetable/public/ru"
TRAIN_LAYER_ID = 5827
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviapars/1.0)",
    "Referer": "https://pass.rzd.ru/timetable/public/ru",
}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_POLL_TRIES = 14
_POLL_INTERVAL_S = 1.5


class RzdDirectAdapter:
    """Unofficial confirmation-tier train prices from pass.rzd.ru's own search
    widget — the two-step RID flow: POST a search, get back
    `{"result": "RID", "RID": N}`, poll the same endpoint with that RID until
    `{"result": "OK", "tp": [...]}` or an error/captcha response. No public
    docs, no auth — this is the same endpoint the site's own JS calls.

    Verified live end-to-end (2026-08-17): a real Moscow->SPb search resolved
    in ~3s (one RID poll), full real fares per car class, no captcha
    triggered. Request/response shapes cross-checked against the reference
    implementation at github.com/devbis/aiorzd, which documents the same flow.

    Exists because Yandex Rasp (the primary train source, `yandex_rasp.py`)
    returns real ticket prices for 0 of the train segments it lists — verified
    across many dates and with/without transport-type filters. This adapter is
    the one place in the roster that actually has RZD's own live prices; the
    plan's §5 "optional, may never be built" framing for `rzd_direct` no
    longer applies now that Yandex Rasp's train pricing gap is confirmed
    structural rather than a query-shape mistake.

    Times: `date0`/`time0`/`date1`/`time1` come back as naive local strings
    with no UTC offset and no per-station tz in the payload. RZD's classic
    (and still dominant) convention is to schedule domestic trains in Moscow
    time — the plan's own timezone note flags exactly this trap — so
    depart/arrive are localized to Europe/Moscow rather than left ambiguous.
    This is a documented simplification, not a verified per-station tz table.
    """

    id = "rzd_direct"
    display_name = "РЖД (pass.rzd.ru)"
    modes = frozenset({Mode.TRAIN})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = frozenset({"RU"})
    supports_batch_dates = False
    request_timeout_s = 15
    # a date here is not one request: the RID flow is a POST plus up to _POLL_TRIES polls
    # paced _POLL_INTERVAL_S apart, so the per-date ceiling is the poll loop's own bound
    # plus one request timeout, and only then multiplied by the dates in a cycle — and by
    # the egress attempts one call may spend replaying that loop through the pool.
    total_timeout_s = probe_dates_total_timeout_s(
        _POLL_TRIES * _POLL_INTERVAL_S + request_timeout_s, egress_attempts=MAX_EGRESS_ATTEMPTS_PER_CALL
    )

    def __init__(
        self,
        *,
        egress_pool: Iterable[str | None] = (),
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 2700,
    ) -> None:
        # §6: RZD gets the most conservative pacing in the roster — the RID flow is the
        # least documented adapter here and the easiest to get rate-limited or captcha'd.
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=20.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._egress = EgressPool(egress_pool)

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.TRAIN:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.rzd_code or not q.dest.rzd_code:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing rzd_code")

        return await self._egress.run(lambda proxy_url: self._search_via(q, proxy_url))

    async def _search_via(self, q: SearchQuery, proxy_url: str | None) -> SearchResult:
        # search() already rejected the query if either rzd_code was missing, so these
        # are never None here — same pattern as travelpayouts._row_to_offer.
        assert q.origin.rzd_code is not None and q.dest.rzd_code is not None
        dates = q.dates or (date.today(),)
        offers: list[Offer] = []
        partial = False
        blocked = False
        connector = ProxyConnector.from_url(proxy_url) if proxy_url else None
        async with aiohttp.ClientSession(headers=_HEADERS, connector=connector) as session:
            for d in dates:
                trains, outcome = await self._search_day(session, q.origin.rzd_code, q.dest.rzd_code, d)
                if outcome == "blocked":
                    blocked = True
                    continue
                if outcome == "error":
                    partial = True
                    continue
                for train in trains:
                    offer = self._train_to_offer(train, q)
                    if offer is not None:
                        offers.append(offer)

        return build_search_result(offers, blocked=blocked, partial=partial)

    async def _post(
        self, session: aiohttp.ClientSession, src: str, dst: str, day: date, rid: int | None
    ) -> tuple[dict[str, Any] | None, int | None]:
        params = {"layer_id": TRAIN_LAYER_ID}
        data = {
            "checkSeats": "1",
            "dir": "0",
            "tfl": "3",
            "dt0": day.strftime("%d.%m.%Y"),
            "code0": str(src),
            "code1": str(dst),
            "ti0": "0-24",
        }
        if rid is not None:
            data["rid"] = str(rid)
        try:
            async with session.post(
                BASE_URL, params=params, data=data, timeout=aiohttp.ClientTimeout(total=self.request_timeout_s)
            ) as resp:
                if resp.status != 200:
                    return None, resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            return None, None
        try:
            result: dict[str, Any] = json.loads(text)
            return result, resp.status
        except ValueError:
            return None, resp.status

    async def _search_day(
        self, session: aiohttp.ClientSession, src: str, dst: str, day: date
    ) -> tuple[list[dict[str, Any]], str]:
        # A 403 is pass.rzd.ru's network-level IP ban (confirmed live — see
        # ARCHITECTURE.md), not a transient upstream hiccup; distinguishing it
        # from a plain non-200/parse failure is what lets /status say "blocked"
        # instead of a generic "error" that looks recoverable on its own.
        response, status = await self._post(session, src, dst, day, rid=None)
        if response is None:
            return [], "blocked" if status == 403 else "error"
        if response.get("error"):
            return [], "blocked"

        rid = response.get("RID")
        for _ in range(_POLL_TRIES):
            if response.get("result") == "OK":
                break
            if rid is None:
                return [], "error"
            await asyncio.sleep(_POLL_INTERVAL_S)
            response, status = await self._post(session, src, dst, day, rid=rid)
            if response is None:
                return [], "blocked" if status == 403 else "error"
            if response.get("error"):
                return [], "blocked"
            rid = response.get("RID", rid)
        else:
            return [], "error"  # never resolved within the poll budget

        tp = response.get("tp") or []
        if not tp:
            return [], "ok"  # legitimate "nothing found" — not a failure
        segment = tp[0]
        msg_list = segment.get("msgList") or []
        if msg_list and msg_list[0].get("errType") == "ERROR":
            return [], "error"
        return segment.get("list") or [], "ok"

    def _train_to_offer(self, train: dict[str, Any], q: SearchQuery) -> Offer | None:
        cars = train.get("cars") or []
        if not cars:
            return None
        cheapest = min(cars, key=lambda c: c["tariff"])

        depart = datetime.strptime(f"{train['date0']} {train['time0']}", "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW_TZ)
        arrive = None
        if train.get("date1") and train.get("time1"):
            arrive = datetime.strptime(f"{train['date1']} {train['time1']}", "%d.%m.%Y %H:%M").replace(
                tzinfo=MOSCOW_TZ
            )

        now = datetime.now(timezone.utc)
        leg = Leg(
            mode=Mode.TRAIN,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=depart.astimezone(timezone.utc),
            depart_local=depart,
            carrier=train.get("carrier"),
            number=train.get("number"),
            arrive_utc=arrive.astimezone(timezone.utc) if arrive else None,
            tz="Europe/Moscow",
            fare_class=cheapest.get("typeLoc") or cheapest.get("type"),
            time_precision="exact",
        )
        duration = int((arrive - depart).total_seconds() // 60) if arrive else None
        itinerary_hash = hashlib.sha256(
            f"{self.id}|{q.origin.rzd_code}|{q.dest.rzd_code}|{train.get('number')}|"
            f"{train['date0']}{train['time0']}".encode()
        ).hexdigest()[:16]

        return Offer(
            legs=(leg,),
            price_minor=round(float(cheapest["tariff"]) * 100),
            currency="RUB",
            deep_link=None,
            source_id=self.id,
            tier=Tier.CONFIRMATION,
            observed_at=now,
            source_cached_at=now,
            expires_at=None,
            confidence=1.0,
            itinerary_hash=itinerary_hash,
            duration_minutes=duration,
        )

    async def health_check(self) -> HealthStatus:
        try:
            async with aiohttp.ClientSession(headers=_HEADERS) as session:
                _, outcome = await self._search_day(session, "2000000", "2004000", date.today())  # Moscow -> SPb
            return HealthStatus(ok=outcome == "ok")
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
