from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.yandex_rasp import YandexRaspAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "yandex_rasp" / "search_mow_led_2026-09-18.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=3, iata="MOW", yandex_code="c213", city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="LED", yandex_code="c2", city_id=None)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> YandexRaspAdapter:
    a = YandexRaspAdapter(api_key="fake-key")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_day(self, session, origin_code, dest_code, day):
        return payload["segments"], True

    monkeypatch.setattr(YandexRaspAdapter, "_fetch_day", fake_fetch_day)
    return a


async def test_segments_without_price_are_skipped(adapter: YandexRaspAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 18),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    assert result.status == ResultStatus.OK
    # fixture has 3 segments, one with tickets_info=null -> only 2 survive
    assert len(result.offers) == 2


async def test_offer_uses_cheapest_place_and_real_leg_detail(adapter: YandexRaspAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 18),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    offer = next(o for o in result.offers if o.legs[0].number == "059Й")
    assert offer.price_minor == 238000  # cheapest place (плацкарт 2380 RUB), not купе
    assert offer.currency == "RUB"
    assert offer.source_cached_at is not None  # live query — age is ~0, not None (see adapter docstring)
    assert offer.duration_minutes == 8 * 60 + 53

    leg = offer.legs[0]
    assert leg.time_precision == "exact"
    assert leg.carrier == "ФПК"
    assert leg.arrive_utc is not None


async def test_price_with_cents_is_converted_correctly(adapter: YandexRaspAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 18),), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)

    offer = next(o for o in result.offers if o.legs[0].number == "756А")
    assert offer.price_minor == 312050  # 3120.50 RUB


async def test_non_train_mode_returns_empty(adapter: YandexRaspAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY
    assert result.offers == []


async def test_missing_yandex_code_returns_empty() -> None:
    a = YandexRaspAdapter(api_key="fake-key")
    origin_no_code = PlaceRef(place_id=1, iata="GOJ", yandex_code=None, city_id=None)
    q = SearchQuery(origin=origin_no_code, dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await a.search(q)
    assert result.status == ResultStatus.EMPTY
