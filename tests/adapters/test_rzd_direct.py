from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.rzd_direct import RzdDirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "rzd_direct" / "search_mow_led_2026-08-27.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=3, iata="MOW", yandex_code="c213", rzd_code="2000000", city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="LED", yandex_code="c2", rzd_code="2004000", city_id=None)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> RzdDirectAdapter:
    a = RzdDirectAdapter()
    final = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calls = {"n": 0}

    async def fake_post(self: RzdDirectAdapter, session: Any, src: str, dst: str, day: date, rid: int | None) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"result": "RID", "RID": 46344281635}
        return final  # type: ignore[no-any-return]

    monkeypatch.setattr(RzdDirectAdapter, "_post", fake_post)
    return a


async def test_rid_flow_resolves_to_offers(adapter: RzdDirectAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 8, 27),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    assert result.status == ResultStatus.OK
    assert len(result.offers) == 3


async def test_offer_uses_cheapest_car_and_real_leg_detail(adapter: RzdDirectAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 8, 27),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    offer = next(o for o in result.offers if o.legs[0].number == "016А")
    assert offer.price_minor == 319900  # cheapest car (Плацкартный 3199 RUB), not Купе/СВ
    assert offer.currency == "RUB"
    assert offer.duration_minutes == 8 * 60 + 29

    leg = offer.legs[0]
    assert leg.time_precision == "exact"
    assert leg.carrier == "ФПК"
    assert leg.tz == "Europe/Moscow"
    assert leg.arrive_utc is not None


async def test_missing_rzd_code_returns_empty() -> None:
    a = RzdDirectAdapter()
    origin_no_code = PlaceRef(place_id=1, iata="GOJ", yandex_code=None, rzd_code=None, city_id=None)
    q = SearchQuery(origin=origin_no_code, dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await a.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_non_train_mode_returns_empty(adapter: RzdDirectAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY
    assert result.offers == []


async def test_captcha_response_maps_to_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    a = RzdDirectAdapter()

    async def fake_post_blocked(self: RzdDirectAdapter, session: Any, src: str, dst: str, day: date, rid: int | None) -> dict[str, Any]:
        return {"error": "captcha required"}

    monkeypatch.setattr(RzdDirectAdapter, "_post", fake_post_blocked)
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 8, 27),), pax=1, mode=Mode.TRAIN)
    result = await a.search(q)
    assert result.status == ResultStatus.BLOCKED
