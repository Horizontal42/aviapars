from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aviapars.adapters.aeroflot_direct import AeroflotDirectAdapter, _as_utc
from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery

FIXTURE = Path(__file__).parent.parent / "fixtures" / "aeroflot_direct" / "search_led_aer.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="AER", yandex_code=None, rzd_code=None, city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 10),), pax=1, mode=Mode.PLANE)


def test_parse_offers_from_real_response() -> None:
    adapter = AeroflotDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 10))

    assert len(offers) == 2
    direct = offers[0]
    assert direct.currency == "RUB"
    assert direct.price_minor == 1350300  # cheapest fare on the direct itinerary, 13503.00 RUB
    assert len(direct.legs) == 1
    leg = direct.legs[0]
    assert leg.carrier == "Россия"  # operating airline, not the marketing brand ("Аэрофлот")
    assert leg.number == "6565"
    assert leg.time_precision == "exact"
    assert leg.tz == "+03:00"
    assert direct.deep_link == (
        "https://www.aeroflot.ru/ru-ru/sb/search?adults=1&infants=0&children=0"
        "&childrenfrgn=0&childrenaward=0&cabin=economy&routes=LED.20260910.AER"
    )

    via_moscow = offers[1]
    assert via_moscow.price_minor == 3551200  # cheapest fare, 35512.00 RUB
    assert len(via_moscow.legs) == 2
    assert via_moscow.legs[0].carrier == "Россия"
    assert via_moscow.legs[1].carrier == "Аэрофлот"


def test_parse_offers_picks_cheapest_fare_not_iteration_order() -> None:
    # `offers` deliberately lists the pricier fare first, so this only passes if the
    # parser actually compares prices instead of taking offers[0].
    payload = {
        "data": {
            "route_itineraries": [
                [
                    {
                        "legs": [
                            {
                                "segments": [
                                    {
                                        "departure": "2026-09-10 08:00",
                                        "departure_utc": "2026-09-10 05:00",
                                        "departure_offset": 180,
                                        "arrival_utc": "2026-09-10 06:30",
                                        "operating_airline_code": "FV",
                                        "operating_airline_name": "Россия",
                                        "operating_flight_number": "100",
                                        "marketing_airline_code": "SU",
                                        "marketing_airline_name": "Аэрофлот",
                                        "marketing_flight_number": "100",
                                    }
                                ]
                            }
                        ],
                        "offers": [
                            {"price_total_amount": "40000.00", "currency_code": "RUB", "brand_code": "EC"},
                            {"price_total_amount": "9000.00", "currency_code": "RUB", "brand_code": "NB"},
                        ],
                    }
                ]
            ]
        }
    }
    adapter = AeroflotDirectAdapter()
    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 10))
    assert len(offers) == 1
    assert offers[0].price_minor == 900000  # 9000 RUB, the cheaper of the two fares


async def test_non_plane_mode_returns_empty() -> None:
    adapter = AeroflotDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_dates_returns_empty() -> None:
    adapter = AeroflotDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_waf_block_returns_blocked_status(monkeypatch: Any) -> None:
    class FakeResp:
        status_code = 503
        text = "<html>blocked</html>"

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.aeroflot_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = AeroflotDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED


def test_as_utc_treats_naive_as_already_utc() -> None:
    naive = datetime(2026, 9, 10, 12, 0)
    assert _as_utc(naive) == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def test_as_utc_converts_aware_instead_of_overwriting_offset() -> None:
    # regression: `.replace(tzinfo=utc)` on an aware datetime silently discards its real
    # offset instead of converting — this proves `_as_utc` converts, not overwrites.
    aware = datetime(2026, 9, 10, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))  # UTC+3
    assert _as_utc(aware) == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
