from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery
from aviapars.adapters.pobeda_direct import PobedaDirectAdapter

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pobeda_direct" / "search_led_svx.json"


def _origin() -> PlaceRef:
    return PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)


def _dest() -> PlaceRef:
    return PlaceRef(place_id=2, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)


def _query() -> SearchQuery:
    return SearchQuery(origin=_origin(), dest=_dest(), dates=(date(2026, 9, 10),), pax=1, mode=Mode.PLANE)


def test_parse_offers_from_real_response() -> None:
    adapter = PobedaDirectAdapter()
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 10))

    assert len(offers) == 3  # one direct + two via-Moscow connections
    direct = next(o for o in offers if len(o.legs) == 1)
    assert direct.currency == "RUB"
    assert direct.price_minor == 899900  # cheapest fare on flight 515, 8999.00 RUB
    leg = direct.legs[0]
    assert leg.carrier == "Pobeda"
    assert leg.number == "515"
    assert leg.time_precision == "exact"
    # LED departs Moscow time (UTC+3), SVX arrives Yekaterinburg time (UTC+5) — not the
    # same offset, so naively treating local time as UTC would be wrong in both directions.
    assert leg.depart_utc == datetime(2026, 9, 10, 20, 35, tzinfo=timezone.utc)
    assert leg.arrive_utc == datetime(2026, 9, 10, 23, 25, tzinfo=timezone.utc)
    assert direct.deep_link == (
        "https://ticket.flypobeda.ru/websky/?origin-city-code%5B0%5D=LED"
        "&destination-city-code%5B0%5D=SVX&date%5B0%5D=10.09.2026&segmentsCount=1"
        "&adultsCount=1&youngAdultsCount=0&childrenCount=0&infantsWithSeatCount=0"
        "&infantsWithoutSeatCount=0&lang=ru"
    )

    connections = [o for o in offers if len(o.legs) == 2]
    assert len(connections) == 2
    assert {o.price_minor for o in connections} == {1309800, 1429800}


async def test_non_plane_mode_returns_empty() -> None:
    adapter = PobedaDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.TRAIN)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_missing_dates_returns_empty() -> None:
    adapter = PobedaDirectAdapter()
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=(), pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)
    assert result.status == ResultStatus.EMPTY


async def test_multiple_dates_each_get_their_own_request(monkeypatch: Any) -> None:
    # regression: search() used to read only q.dates[0] and silently drop the rest of
    # whatever window the combiner handed it (up to flex_probe_dates_per_cycle dates per
    # cycle, see domain/dates.py).
    seen_dates: list[str] = []

    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            return {"result": "ok", "flights": [], "prices": [{}]}

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            seen_dates.append(kwargs["data"]["date[0]"])
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.pobeda_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = PobedaDirectAdapter()
    dates = (date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert seen_dates == ["10.09.2026", "11.09.2026", "12.09.2026"]
    assert result.status == ResultStatus.EMPTY  # each date legitimately empty, not a failure


async def test_waf_block_returns_blocked_status(monkeypatch: Any) -> None:
    class FakeResp:
        status_code = 403
        text = "<html>your ip failed verification</html>"

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr("aviapars.adapters.pobeda_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = PobedaDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED


async def test_proxy_url_forwarded_to_session(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_session(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class FakeResp:
            status_code = 200

            def json(self) -> Any:
                return {"result": "ok", "flights": [], "prices": [{}]}

        class FakeSession:
            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def post(self, *args: Any, **kwargs: Any) -> FakeResp:
                return FakeResp()

        return FakeSession()

    monkeypatch.setattr("aviapars.adapters.pobeda_direct.AsyncSession", fake_session)

    adapter = PobedaDirectAdapter(egress_pool=["socks5://100.64.0.1:11080"])
    await adapter.search(_query())
    assert captured.get("proxy") == "socks5://100.64.0.1:11080"
