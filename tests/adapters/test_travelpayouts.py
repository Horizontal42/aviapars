from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.travelpayouts import TravelpayoutsAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "travelpayouts" / "month_matrix_svx_aer_2026-09.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="AER", yandex_code=None, rzd_code=None, city_id=None)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> TravelpayoutsAdapter:
    a = TravelpayoutsAdapter(token="fake-token", marker="732612")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)
    return a


async def test_only_direct_flights_are_kept(adapter: TravelpayoutsAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert result.status == ResultStatus.OK
    # fixture has 5 rows: 3 with number_of_changes=0, 2 with 1 — only the 3 survive
    assert len(result.offers) == 3
    assert all(len(o.legs) == 1 for o in result.offers)


async def test_offer_fields_are_honest_about_missing_detail(adapter: TravelpayoutsAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 29),), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.price_minor == 610100  # 6101 RUB -> kopecks
    assert offer.currency == "RUB"
    assert offer.expires_at is None  # month-matrix never gives one — see plan §7
    assert offer.source_cached_at is not None  # mapped from found_at
    assert offer.duration_minutes == 240
    assert "marker=732612" in (offer.deep_link or "")

    leg = offer.legs[0]
    assert leg.time_precision == "date_only"
    assert leg.carrier is None
    assert leg.number is None
    assert leg.arrive_utc is None


async def test_date_filter_narrows_to_requested_dates(adapter: TravelpayoutsAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 25),), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert len(result.offers) == 1
    assert result.offers[0].legs[0].depart_utc.date() == date(2026, 9, 25)


async def test_non_plane_mode_returns_empty(adapter: TravelpayoutsAdapter) -> None:
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY
    assert result.offers == []
