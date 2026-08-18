from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.tutu_avia_direct import TutuAviaDirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tutu_avia_direct" / "search_mow_svx.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 20),), pax=1, mode=Mode.PLANE)


def test_parse_offers_from_real_response() -> None:
    adapter = TutuAviaDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 20))

    assert len(offers) == 3
    assert sorted(o.price_minor for o in offers) == [1026700, 1042000, 1042000]
    cheapest = min(offers, key=lambda o: o.price_minor)
    assert cheapest.currency == "RUB"
    assert cheapest.price_minor == 1026700
    leg = cheapest.legs[0]
    assert leg.carrier == "Уральские авиалинии"
    assert leg.number == "U6-299"
    assert leg.depart_utc.isoformat() == "2026-09-20T14:45:00+00:00"
    assert leg.arrive_utc is not None
    assert leg.arrive_utc.isoformat() == "2026-09-20T17:10:00+00:00"


async def test_non_plane_mode_returns_empty() -> None:
    adapter = TutuAviaDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_iata_returns_empty() -> None:
    adapter = TutuAviaDirectAdapter()
    no_iata = PlaceRef(place_id=3, iata=None, yandex_code=None, rzd_code=None, city_id=None)
    q = SearchQuery(origin=no_iata, dest=_dest(), dates=(date(2026, 9, 20),), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_city_id_cache_avoids_repeat_suggest_calls() -> None:
    calls = {"suggest": 0}

    class FakeResp:
        status_code = 200

        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def json(self) -> Any:
            return self._payload

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> FakeResp:
            calls["suggest"] += 1
            iata = kwargs["params"]["name"]
            return FakeResp({"suggestions": [{"city": {"id": "1", "code": iata}}]})

    adapter = TutuAviaDirectAdapter()
    async with FakeSession() as session:  # type: ignore[misc]
        first = await adapter._resolve_city_id(session, "MOW")
        second = await adapter._resolve_city_id(session, "MOW")
    assert first == second == "1"
    assert calls["suggest"] == 1


async def test_blocked_status_on_403(monkeypatch: Any) -> None:
    class FakeSuggestResp:
        status_code = 200

        def __init__(self, iata: str) -> None:
            self._iata = iata

        def json(self) -> Any:
            return {"suggestions": [{"city": {"id": "1", "code": self._iata}}]}

    class FakeBlockedResp:
        status_code = 403

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> FakeSuggestResp:
            return FakeSuggestResp(kwargs["params"]["name"])

        async def post(self, *args: Any, **kwargs: Any) -> FakeBlockedResp:
            return FakeBlockedResp()

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED
