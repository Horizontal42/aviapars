from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.tutu_rail_direct import TutuRailDirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tutu_rail_direct" / "search_mow_led.json"


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

    # fixture has 4 offers merged across two response chunks (2 unique to chunk0,
    # 1 overlapping, 1 unique to chunk1) but 2 of them only have "absent"-priced
    # (sold-out/waitlist) variants and must be dropped
    assert len(offers) == 2
    assert {o.price_minor for o in offers} == {514106, 581025}
    priced = next(o for o in offers if o.price_minor == 514106)
    assert priced.currency == "RUB"
    leg = priced.legs[0]
    assert leg.carrier == "ФПК"
    assert leg.number == "002А"
    assert leg.depart_utc.isoformat() == "2026-09-20T20:55:00+00:00"
    assert leg.arrive_utc is not None
    assert leg.arrive_utc.isoformat() == "2026-09-21T04:55:00+00:00"


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
