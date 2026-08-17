from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from aviapars.adapters.azimuth_direct import AzimuthDirectAdapter
from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery

FIXTURE = Path(__file__).parent.parent / "fixtures" / "azimuth_direct" / "search_led_mow.json"


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
