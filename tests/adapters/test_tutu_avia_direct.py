from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from curl_cffi.requests.exceptions import Timeout

from aviapars.adapters.base import Mode, PlaceRef, ResultStatus, SearchQuery, Tier
from aviapars.adapters.tutu_avia_direct import TutuAviaDirectAdapter, _deep_link

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tutu_avia_direct" / "search_mow_svx.json"
APPROXIMATE_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "tutu_avia_direct" / "search_mow_svx_approximate.json"
)


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
    assert cheapest.tier == Tier.CONFIRMATION
    leg = cheapest.legs[0]
    assert leg.carrier == "Уральские авиалинии"
    assert leg.number == "U6-299"
    assert leg.depart_utc.isoformat() == "2026-09-20T14:45:00+00:00"
    assert leg.arrive_utc is not None
    assert leg.arrive_utc.isoformat() == "2026-09-20T17:10:00+00:00"
    # regression: the response's local arrival was parsed and then dropped, so the deal
    # card could only ever show a departure — the owner reported exactly that gap.
    # 22:10 is Yekaterinburg time (UTC+5), i.e. the *destination's* clock. Deriving an
    # arrival from arrive_utc plus the leg's `tz` (the departure offset, Moscow +3) would
    # have printed 20:10 here — two hours wrong, which is why nothing derives it.
    assert leg.arrive_local is not None
    assert leg.arrive_local.strftime("%H:%M") == "22:10"


def test_approximate_price_used_with_reduced_confidence_when_no_exact_variant() -> None:
    # regression: tutu_rail_direct proved live that a route/date can come back with zero
    # "exact" variants (booking not open yet) — same platform, same offerVariants shape,
    # so avia gets the identical fallback. Fixture: cheapest offer's variants mutated to
    # "approximate", the other two left "exact", to prove both paths coexist correctly.
    adapter = TutuAviaDirectAdapter()
    payload = json.loads(APPROXIMATE_FIXTURE.read_text(encoding="utf-8"))

    offers = adapter._parse_offers(payload, _query(), date(2026, 9, 20))

    assert len(offers) == 3
    approx = next(o for o in offers if o.price_minor == 1026700)
    assert approx.confidence == 0.5
    # not CONFIRMATION: an "approximate" fare isn't bookable, and the bot's confirmed/✅
    # display checks tier alone — this must not be shown as a confirmed real price.
    assert approx.tier == Tier.DISCOVERY
    exact = [o for o in offers if o.price_minor == 1042000]
    assert len(exact) == 2
    assert all(o.confidence == 1.0 for o in exact)
    assert all(o.tier == Tier.CONFIRMATION for o in exact)


def test_deep_link_matches_verified_query_shape() -> None:
    # route[0]=<cityId>-<DDMMYYYY>-<cityId> captured from a live browser session
    # (avia.tutu.ru.har's page URL) — must not drift from that verified format.
    link = _deep_link(_query(), origin_id="491", dest_id="29", depart_date=date(2026, 9, 20))
    assert link == "https://avia.tutu.ru/f/?class=Y&travelers=1&route%5B0%5D=491-20092026-29"


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
    assert first == second == ("1", "ok")
    assert calls["suggest"] == 1


async def test_multiple_dates_each_get_their_own_request(monkeypatch: Any) -> None:
    # regression: search() used to read only q.dates[0] and silently drop the rest of
    # whatever window the combiner handed it (up to flex_probe_dates_per_cycle dates per
    # cycle, see domain/dates.py) — it must issue one request per date and merge results.
    seen_dates: list[str] = []
    suggest_calls = {"n": 0}

    class FakeSuggestResp:
        status_code = 200

        def __init__(self, iata: str) -> None:
            self._iata = iata

        def json(self) -> Any:
            return {"suggestions": [{"city": {"id": "1", "code": self._iata}}]}

    class FakeOffersResp:
        status_code = 200

        def json(self) -> Any:
            return [{"dictionary": {"common": {}}, "offers": {"actual": {}}}]

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> FakeSuggestResp:
            suggest_calls["n"] += 1
            return FakeSuggestResp(kwargs["params"]["name"])

        async def post(self, *args: Any, **kwargs: Any) -> FakeOffersResp:
            seen_dates.append(json.loads(kwargs["data"])["routes"][0]["departureDate"])
            return FakeOffersResp()

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    dates = (date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22))
    q = SearchQuery(origin=_origin(), dest=_dest(), dates=dates, pax=1, mode=Mode.PLANE)
    result = await adapter.search(q)

    assert seen_dates == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert suggest_calls["n"] == 2  # city ids cached — resolved once, not once per date
    assert result.status == ResultStatus.EMPTY  # each date legitimately empty, not a failure


async def test_city_ids_sent_as_json_integers_not_strings(monkeypatch: Any) -> None:
    # regression: Tutu's backend now rejects (instant empty-body 500) routes[].
    # departureCityId/arrivalCityId sent as JSON strings - verified live that casting
    # them to int is the only change needed to get a 200 with real offers back.
    seen_bodies: list[dict[str, Any]] = []

    class FakeSuggestResp:
        status_code = 200

        def __init__(self, iata: str) -> None:
            self._iata = iata

        def json(self) -> Any:
            return {"suggestions": [{"city": {"id": "491", "code": self._iata}}]}

    class FakeOffersResp:
        status_code = 200

        def json(self) -> Any:
            return [{"dictionary": {"common": {}}, "offers": {"actual": {}}}]

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> FakeSuggestResp:
            return FakeSuggestResp(kwargs["params"]["name"])

        async def post(self, *args: Any, **kwargs: Any) -> FakeOffersResp:
            seen_bodies.append(json.loads(kwargs["data"]))
            return FakeOffersResp()

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    await adapter.search(_query())

    assert len(seen_bodies) == 1
    route = seen_bodies[0]["routes"][0]
    assert route["departureCityId"] == 491 and isinstance(route["departureCityId"], int)
    assert route["arrivalCityId"] == 491 and isinstance(route["arrivalCityId"], int)


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


async def test_suggester_connection_failure_is_blocked_not_city_not_found(monkeypatch: Any) -> None:
    # The suggester lives on the same banned tutu.ru infrastructure as the offers
    # endpoint. Collapsing "the connection never completed" into the same bare None as
    # "Tutu has no such city" reported EMPTY — which tells the egress pool this way out
    # works and pins the adapter to a dead one.
    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> Any:
            raise Timeout("Failed to perform, curl: (28) Connection timed out after 20001 ms")

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED


async def test_unknown_city_still_reports_empty(monkeypatch: Any) -> None:
    class FakeSuggestResp:
        status_code = 200

        def json(self) -> Any:
            return {"suggestions": []}

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> FakeSuggestResp:
            return FakeSuggestResp()

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.EMPTY
    assert result.error == "city not found"


async def test_connection_timeout_on_offers_maps_to_blocked(monkeypatch: Any) -> None:
    class FakeSuggestResp:
        status_code = 200

        def __init__(self, iata: str) -> None:
            self._iata = iata

        def json(self) -> Any:
            return {"suggestions": [{"city": {"id": "1", "code": self._iata}}]}

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> FakeSuggestResp:
            return FakeSuggestResp(kwargs["params"]["name"])

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise Timeout("Failed to perform, curl: (28) Connection timed out after 20001 ms")

    monkeypatch.setattr("aviapars.adapters.tutu_avia_direct.AsyncSession", lambda **kw: FakeSession())

    adapter = TutuAviaDirectAdapter()
    result = await adapter.search(_query())
    assert result.status == ResultStatus.BLOCKED
