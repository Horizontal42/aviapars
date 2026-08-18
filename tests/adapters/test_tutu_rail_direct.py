from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

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
