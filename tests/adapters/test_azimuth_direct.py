from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from aviapars.adapters.azimuth_direct import AzimuthDirectAdapter
from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery

FIXTURE = Path(__file__).parent.parent / "fixtures" / "azimuth_direct" / "search_led_mow.json"
MOSCOW = ZoneInfo("Europe/Moscow")  # both endpoints of the fixture flight (LED→MOW)


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 19),), pax=1, mode=Mode.PLANE)


def test_parse_offers_from_real_response() -> None:
    adapter = AzimuthDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 19))

    assert len(offers) == 1  # one flight that date, cheapest of its 4 fare options
    offer = offers[0]
    assert offer.currency == "RUB"
    assert offer.price_minor == 678000  # cheapest fare ("Легкий"), 6780.00 RUB
    assert len(offer.legs) == 1
    leg = offer.legs[0]
    assert leg.carrier == "Азимут"
    assert leg.number == "7020"
    assert leg.time_precision == "exact"
    # same gap as tutu_avia: arrival_date/arrival_time were in the payload all along and
    # never reached the Leg, so no arrival time could be rendered
    assert leg.arrive_local is not None
    assert leg.arrive_local.strftime("%d.%m %H:%M") == leg.arrive_utc.astimezone(MOSCOW).strftime("%d.%m %H:%M")


def test_parse_offers_ignores_directions_for_other_dates() -> None:
    adapter = AzimuthDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # fixture's second `directions` entry (21.09.2026) has no `flights` key at all
    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 21))
    assert offers == []


def test_parse_offers_picks_cheapest_fare_not_iteration_order() -> None:
    payload = [
        {
            "method": "get-availability",
            "result": {
                "directions": [
                    {
                        "date": "19.09.2026",
                        "flights": [
                            {
                                "segments": [
                                    {
                                        "ak_full_name": "Азимут",
                                        "flight_number": "100",
                                        "departure_date": "19.09.2026",
                                        "departure_time": "07:30",
                                        "departure_utc": "19.09.2026 04:30:00",
                                        "arrival_utc": "19.09.2026 06:00:00",
                                    }
                                ],
                                "offers": [
                                    {"price": "40000.00", "marketing_fare_code2": "FLEX"},
                                    {"price": "9000.00", "marketing_fare_code2": "LIGHT"},
                                ],
                            }
                        ],
                    }
                ]
            },
        }
    ]
    adapter = AzimuthDirectAdapter()
    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 19))
    assert len(offers) == 1
    assert offers[0].price_minor == 900000  # 9000 RUB, the cheaper of the two fares


async def test_non_plane_mode_returns_empty() -> None:
    adapter = AzimuthDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_dates_returns_empty() -> None:
    adapter = AzimuthDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_multiple_dates_reuse_one_init_token(monkeypatch: Any) -> None:
    # regression: search() used to read only q.dates[0] and silently drop the rest of
    # whatever window the combiner handed it (up to flex_probe_dates_per_cycle dates per
    # cycle, see domain/dates.py). Also proves init is called once, not once per date —
    # the token is a one-time-cheap-extra-request thing, not per-date state.
    seen_dates: list[str] = []
    calls = {"n": 0}

    class FakeInitResp:
        status_code = 200
        headers = {"Authorization": "Bearer test-token"}

    class FakeAvailResp:
        status_code = 200

        def json(self) -> Any:
            return [{"method": "get-availability", "result": {"directions": []}}]

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeInitResp()
            seen_dates.append(kwargs["json"][0][1]["outbound_date"])
            return FakeAvailResp()

    monkeypatch.setattr("aviapars.adapters.azimuth_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = AzimuthDirectAdapter()
    dates = (date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert calls["n"] == 4  # 1 init + 3 get-availability
    assert seen_dates == ["10.09.2026", "11.09.2026", "12.09.2026"]
    assert result.status == ResultStatus.EMPTY  # each date legitimately empty, not a failure


async def test_init_failure_returns_blocked(monkeypatch: Any) -> None:
    class FakeResp:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> Any:
            return []

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.azimuth_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = AzimuthDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED
