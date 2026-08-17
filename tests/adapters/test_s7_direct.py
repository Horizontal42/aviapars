from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.s7_direct import S7DirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "s7_direct" / "search_result_raw.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="LED", yandex_code=None, rzd_code=None, city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 8, 19),), pax=1, mode=Mode.PLANE)


def test_parse_offers_from_real_response() -> None:
    adapter = S7DirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 8, 19))

    assert len(offers) == 3
    offer = offers[0]
    assert offer.currency == "RUB"
    assert offer.price_minor == 2679900  # cheapest available fare family (BASICECONOMY), 26799 RUB
    assert len(offer.legs) == 2  # this flight option is a one-stop connection
    leg = offer.legs[0]
    assert leg.carrier == "S7 Airlines"
    assert leg.number == "1196"
    assert leg.time_precision == "exact"
    assert leg.tz == "Europe/Moscow"
    assert offer.duration_minutes == 790


def test_parse_offers_picks_cheapest_by_price_not_iteration_order() -> None:
    # dict insertion order deliberately puts the *pricier* solution first, so this only
    # passes if the parser actually compares prices instead of taking solutions[0].
    payload = {
        "optionSets": [
            {
                "flightOptions": [
                    {
                        "id": "fo-1",
                        "route": {
                            "duration": {"amountInMinutes": 120},
                            "segments": [
                                {
                                    "departureDate": "2026-08-19T10:00:00",
                                    "departureTimeZone": "Europe/Moscow",
                                    "arrivalDate": "2026-08-19T12:00:00",
                                    "operatingAirline": {"name": "S7 Airlines", "flightNumber": "100"},
                                }
                            ],
                        },
                        "solutions": {
                            "PLUSECONOMY": {
                                "fareFamily": "PLUSECONOMY",
                                "available": True,
                                "pricing": {"total": {"salePrice": {"amount": 40000, "currency": "RUB"}}},
                            },
                            "BASICECONOMY": {
                                "fareFamily": "BASICECONOMY",
                                "available": True,
                                "pricing": {"total": {"salePrice": {"amount": 9000, "currency": "RUB"}}},
                            },
                            "SOLDOUT_CHEAPEST": {
                                "fareFamily": "SOLDOUT_CHEAPEST",
                                "available": False,
                                "pricing": {"total": {"salePrice": {"amount": 1, "currency": "RUB"}}},
                            },
                        },
                    }
                ]
            }
        ]
    }
    adapter = S7DirectAdapter()
    offers = adapter._parse_offers(payload, _query(), date(2026, 8, 19))
    assert len(offers) == 1
    assert offers[0].price_minor == 900000  # BASICECONOMY (9000 RUB) — cheapest *available* fare


async def test_non_plane_mode_returns_empty() -> None:
    adapter = S7DirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_dates_returns_empty() -> None:
    adapter = S7DirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_cheap_path_success_never_invokes_browser_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = S7DirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offers = adapter._parse_offers(payload, _query(), date(2026, 8, 19))

    async def fake_resolve(self: S7DirectAdapter, session: Any, iata: str) -> str:
        return f"loc-{iata}"

    async def fake_try_search(self: S7DirectAdapter, *args: Any, **kwargs: Any) -> tuple[list[Any], bool]:
        return offers, True

    async def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("browser solver must not be invoked when the cheap path already succeeded")

    monkeypatch.setattr(S7DirectAdapter, "_resolve_location_id", fake_resolve)
    monkeypatch.setattr(S7DirectAdapter, "_try_search", fake_try_search)
    monkeypatch.setattr(adapter._solver, "solve", fail_if_called)

    result = await adapter.search(_query())
    assert result.status == ResultStatus.OK
    assert len(result.offers) == 3


async def test_falls_back_to_browser_solver_when_challenge_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = S7DirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offers = adapter._parse_offers(payload, _query(), date(2026, 8, 19))
    calls = {"try_search": 0}

    async def fake_resolve(self: S7DirectAdapter, session: Any, iata: str) -> str:
        return f"loc-{iata}"

    async def fake_try_search(self: S7DirectAdapter, *args: Any, **kwargs: Any) -> tuple[list[Any], bool]:
        calls["try_search"] += 1
        if calls["try_search"] == 1:
            return [], False  # blocked by challenge
        return offers, True  # succeeds after a fresh cookie set

    async def fake_solve(target: str, params: dict[str, str]) -> dict[str, Any]:
        return {"cookies": {"cfidsw-s7-ibe": "fresh-token"}, "user_agent": "x"}

    saved: dict[str, Any] = {}

    async def fake_setter(payload_: dict[str, Any], expires_at: datetime) -> None:
        saved["payload"] = payload_
        saved["expires_at"] = expires_at

    monkeypatch.setattr(S7DirectAdapter, "_resolve_location_id", fake_resolve)
    monkeypatch.setattr(S7DirectAdapter, "_try_search", fake_try_search)
    monkeypatch.setattr(adapter._solver, "solve", fake_solve)
    adapter._session_setter = fake_setter

    result = await adapter.search(_query())
    assert result.status == ResultStatus.OK
    assert calls["try_search"] == 2
    assert saved["payload"]["cookies"]["cfidsw-s7-ibe"] == "fresh-token"


async def test_blocked_when_browser_solver_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = S7DirectAdapter()

    async def fake_resolve(self: S7DirectAdapter, session: Any, iata: str) -> str:
        return f"loc-{iata}"

    async def fake_try_search(self: S7DirectAdapter, *args: Any, **kwargs: Any) -> tuple[list[Any], bool]:
        return [], False

    async def fake_solve(target: str, params: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(S7DirectAdapter, "_resolve_location_id", fake_resolve)
    monkeypatch.setattr(S7DirectAdapter, "_try_search", fake_try_search)
    monkeypatch.setattr(adapter._solver, "solve", fake_solve)

    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED
