from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from curl_cffi.requests.exceptions import Timeout

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery, Tier
from aviapars.adapters.tutu_rail_direct import TutuRailDirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tutu_rail_direct" / "search_mow_led.json"
APPROXIMATE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "tutu_rail_direct" / "search_mow_led_approximate.json"
)


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata=None, yandex_code=None, rzd_code="2000000", city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata=None, yandex_code=None, rzd_code="2004000", city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 20),), pax=1, mode=Mode.TRAIN)


def test_parse_offers_from_real_response() -> None:
    adapter = TutuRailDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 20))

    # fixture has 4 unique offers merged across two response chunks (2 unique to chunk0,
    # 1 overlapping, 1 unique to chunk1). The overlapping one resolves to chunk1's later
    # snapshot (dict.update semantics), which has 3 "absent" variants and 1 "approximate"
    # — kept via the approximate fallback at reduced confidence. The other chunk1-only
    # offer is "absent"-only and correctly dropped, leaving 3 of the 4.
    assert len(offers) == 3
    assert {o.price_minor for o in offers} == {514106, 581025, 846464}
    approx = next(o for o in offers if o.price_minor == 846464)
    assert approx.confidence == 0.5
    # not CONFIRMATION: an "approximate" fare isn't bookable, and the bot's confirmed/✅
    # display checks tier alone — this must not be shown as a confirmed real price.
    assert approx.tier == Tier.DISCOVERY
    priced = next(o for o in offers if o.price_minor == 514106)
    assert priced.confidence == 1.0
    assert priced.tier == Tier.CONFIRMATION
    assert priced.currency == "RUB"
    leg = priced.legs[0]
    assert leg.carrier == "ФПК"
    assert leg.number == "002А"
    assert leg.depart_utc.isoformat() == "2026-09-20T20:55:00+00:00"
    assert leg.arrive_utc is not None
    assert leg.arrive_utc.isoformat() == "2026-09-21T04:55:00+00:00"


def test_approximate_price_used_with_reduced_confidence_when_no_exact_offer() -> None:
    # regression: live-captured 30-days-out response where every offer's variants were
    # "approximate" (ticket sales not yet open) or "absent" (sold out) — zero "exact".
    # The old exact-only filter dropped every one of these, returning EMPTY for a route
    # that genuinely has trains running; this proves the fallback keeps them, flagged.
    adapter = TutuRailDirectAdapter()
    payload = json.loads(APPROXIMATE_FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 17))

    assert len(offers) == 2
    assert all(o.confidence == 0.5 for o in offers)
    assert all(o.tier == Tier.DISCOVERY for o in offers)
    assert sorted(o.price_minor for o in offers) == [90337, 281962]


async def test_non_train_mode_returns_empty() -> None:
    adapter = TutuRailDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_rzd_code_returns_empty() -> None:
    adapter = TutuRailDirectAdapter()
    no_code = PlaceRef(place_id=3, iata=None, yandex_code=None, rzd_code=None, city_id=None)
    q = SearchQuery(origin=no_code, dest=_dest(), dates=(date(2026, 9, 20),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


def test_null_or_zero_fraction_does_not_crash() -> None:
    # regression: `.get("fraction", 100)` doesn't defend against an explicit `null` (still
    # returns None, not the default) or a `fraction: 0` (ZeroDivisionError) — both are real
    # shapes Tutu's own platform is known to send (see _tutu_common.py).
    segment_id = "seg1"
    route_id = "route1"
    payload = [
        {
            "dictionary": {
                "common": {
                    "routes": {route_id: {"id": route_id, "segmentIds": [segment_id], "transferIds": []}},
                    "segments": {
                        segment_id: {
                            "id": segment_id,
                            "departureDateTime": "2026-09-20T10:00:00+03:00",
                            "arrivalDateTime": "2026-09-20T12:00:00+03:00",
                            "carriers": [],
                            "voyageNumber": "",
                        }
                    },
                    "carriers": {},
                },
                "train": {"voyages": {}},
            },
            "offers": {
                "actual": {
                    "null-fraction": {
                        "routeIds": [route_id],
                        "offerVariants": [
                            {"price": {"type": "exact", "value": {"amount": 100000, "fraction": None}}}
                        ],
                    },
                    "zero-fraction": {
                        "routeIds": [route_id],
                        "offerVariants": [{"price": {"type": "exact", "value": {"amount": 50000, "fraction": 0}}}],
                    },
                }
            },
        }
    ]

    adapter = TutuRailDirectAdapter()
    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 20))

    assert len(offers) == 2
    assert sorted(o.price_minor for o in offers) == [50000, 100000]


async def test_multiple_dates_each_get_their_own_request(monkeypatch: Any) -> None:
    # regression: search() used to read only q.dates[0] and silently drop the rest of
    # whatever window the combiner handed it (up to flex_probe_dates_per_cycle dates per
    # cycle, see domain/dates.py) — it must issue one request per date and merge results.
    seen_dates: list[str] = []

    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            return [{"dictionary": {"common": {}, "train": {}}, "offers": {"actual": {}}}]

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            seen_dates.append(kwargs["json"]["routes"][0]["departureDate"])
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuRailDirectAdapter()
    dates = (date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    assert seen_dates == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert result.status == ResultStatus.EMPTY  # each date legitimately empty, not a failure


async def test_one_blocked_date_does_not_sink_the_others(monkeypatch: Any) -> None:
    class FakeResp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def json(self) -> Any:
            return [{"dictionary": {"common": {}, "train": {}}, "offers": {"actual": {}}}]

    class FakeSession:
        def __init__(self) -> None:
            self._calls = 0

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            self._calls += 1
            return FakeResp(403 if self._calls == 1 else 200)

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuRailDirectAdapter()
    dates = (date(2026, 9, 20), date(2026, 9, 21))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    # both dates are still queried (proves one blocked date doesn't abort the loop) — with
    # no offers found on either, BLOCKED is the honest status: rzd_direct's same status
    # priority (any offers > blocked > error > empty) applies here too.
    assert result.status == ResultStatus.BLOCKED


async def test_blocked_status_on_403(monkeypatch: Any) -> None:
    class FakeResp:
        status_code = 403

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuRailDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED


async def test_connection_timeout_maps_to_blocked_not_upstream_error(monkeypatch: Any) -> None:
    # tutu.ru's network-level ban drops the SYN silently — there is no 403 to read
    # (verified live 2026-08-19 from the VDS). Classifying that as UPSTREAM_ERROR left
    # the egress pool with no signal that the way out, not the site, was the problem.
    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise Timeout("Failed to perform, curl: (28) Connection timed out after 20001 ms")

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuRailDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED


async def test_parse_failure_stays_an_upstream_error(monkeypatch: Any) -> None:
    # The counterpart: the egress worked and the payload changed. Rotating IPs for that
    # would burn a second request on the same bug.
    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            raise ValueError("Expecting value: line 1 column 1")

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuRailDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.UPSTREAM_ERROR


async def test_blocked_egress_rotates_to_the_next_one(monkeypatch: Any) -> None:
    proxies_tried: list[str | None] = []

    class FakeResp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def json(self) -> Any:
            return [{"dictionary": {"common": {}, "train": {}}, "offers": {"actual": {}}}]

    def fake_session(**kwargs: Any) -> Any:
        proxy = kwargs.get("proxy")
        proxies_tried.append(proxy)

        class FakeSession:
            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, *args: Any, **kw: Any) -> FakeResp:
                return FakeResp(200 if proxy == "socks5://127.0.0.1:10808" else 403)

        return FakeSession()

    monkeypatch.setattr("aviapars.adapters.tutu_rail_direct.AsyncSession", fake_session)

    adapter = TutuRailDirectAdapter(egress_pool=[None, "socks5://127.0.0.1:10808"])
    result = await adapter.search(_query())

    assert proxies_tried == [None, "socks5://127.0.0.1:10808"]
    assert result.status == ResultStatus.EMPTY  # the second egress answered, just with nothing

    # and the block is remembered: the next search skips the direct egress entirely
    proxies_tried.clear()
    await adapter.search(_query())
    assert proxies_tried == ["socks5://127.0.0.1:10808"]
