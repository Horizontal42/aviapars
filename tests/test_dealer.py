from __future__ import annotations

from datetime import datetime, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.db.models import Route
from aviapars.domain.combiner import Itinerary
from aviapars.domain.dealer import crosses_threshold, threshold_for
from aviapars.scheduler.worker import _deals_to_send

_A = PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
_B = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _leg(mode: Mode) -> Leg:
    return Leg(
        mode=mode,
        from_place=_A,
        to_place=_B,
        depart_utc=_NOW,
        depart_local=_NOW,
        carrier=None,
        number=None,
        arrive_utc=_NOW,
        time_precision="exact",
    )


def _itinerary(*modes: Mode, price_minor: int, currency: str = "RUB") -> Itinerary:
    offers = tuple(
        Offer(
            legs=(_leg(m),),
            price_minor=price_minor // len(modes),
            currency=currency,
            deep_link=None,
            source_id="src",
            tier=Tier.CONFIRMATION,
            observed_at=_NOW,
            source_cached_at=_NOW,
            expires_at=None,
            confidence=1.0,
            itinerary_hash=f"h{m.value}{price_minor}",
        )
        for m in modes
    )
    return Itinerary(offers=offers, price_minor=price_minor, currency=currency)


def _route(*, max_price: int, by_mode: dict[str, int] | None = None) -> Route:
    return Route(
        id=1,
        user_id=1,
        title="LED → MOW",
        origin_place_id=1,
        dest_place_id=2,
        mode_pref="auto",
        date_mode="fixed",
        fixed_dates=["2026-09-10"],
        window_start_date=None,
        window_start_offset_days=None,
        window_length_days=None,
        date_cursor=0,
        max_price=max_price,
        max_price_currency="RUB",
        max_price_by_mode=by_mode,
        pax=1,
        allow_hub_routing=False,
        hub_place_ids=None,
        max_hops=0,
        max_transfer_min=None,
        max_total_hours=None,
        poll_interval_min=60,
        notification_mode="instant",
        active=True,
        next_run_at=None,
        last_run_at=None,
        created_at="",
    )


def test_shared_ceiling_ignores_modes_entirely() -> None:
    route = _route(max_price=800000)

    assert threshold_for(route, [Mode.PLANE]) == 800000
    assert threshold_for(route, [Mode.TRAIN]) == 800000
    assert threshold_for(route) == 800000


def test_per_mode_ceiling_applies_to_a_single_mode_itinerary() -> None:
    route = _route(max_price=800000, by_mode={"plane": 800000, "train": 400000})

    assert threshold_for(route, [Mode.PLANE]) == 800000
    assert threshold_for(route, [Mode.TRAIN]) == 400000
    # a same-mode hub trip is still "a plane price" — two plane legs, one ceiling
    assert threshold_for(route, [Mode.PLANE, Mode.PLANE]) == 800000


def test_mixed_mode_itinerary_falls_back_to_the_shared_ceiling() -> None:
    # a plane leg plus a train leg is neither a plane price nor a train price, so neither
    # per-mode ceiling describes it — max_price (the larger of the two) is the bound.
    route = _route(max_price=800000, by_mode={"plane": 800000, "train": 400000})

    assert threshold_for(route, [Mode.PLANE, Mode.TRAIN]) == 800000


def test_crosses_threshold_uses_the_mode_specific_ceiling() -> None:
    route = _route(max_price=800000, by_mode={"plane": 800000, "train": 400000})

    assert crosses_threshold(route, _itinerary(Mode.PLANE, price_minor=700000), [Mode.PLANE])
    assert not crosses_threshold(route, _itinerary(Mode.TRAIN, price_minor=700000), [Mode.TRAIN])
    assert crosses_threshold(route, _itinerary(Mode.TRAIN, price_minor=350000), [Mode.TRAIN])


def test_currency_mismatch_never_qualifies() -> None:
    route = _route(max_price=800000)

    assert not crosses_threshold(route, _itinerary(Mode.PLANE, price_minor=1, currency="USD"), [Mode.PLANE])


def test_deal_selection_scans_past_a_cheaper_non_qualifying_itinerary() -> None:
    # regression: only outcome.itineraries[0] was ever checked. With per-mode ceilings the
    # globally cheapest can be a train above the train ceiling, sitting in front of a plane
    # that is under the plane ceiling — that plane deal was silently lost.
    route = _route(max_price=800000, by_mode={"plane": 800000, "train": 400000})
    train = _itinerary(Mode.TRAIN, price_minor=500000)
    plane = _itinerary(Mode.PLANE, price_minor=700000)

    assert _deals_to_send(route, [train, plane]) == [plane]


def test_per_mode_ceilings_notify_once_per_transport_group() -> None:
    # "planes under 8000 OR trains under 4000" is two asks; answering only the cheaper one
    # would leave a qualifying plane deal invisible while any cheap train exists.
    route = _route(max_price=800000, by_mode={"plane": 800000, "train": 400000})
    train = _itinerary(Mode.TRAIN, price_minor=300000)
    train_dearer = _itinerary(Mode.TRAIN, price_minor=350000)
    plane = _itinerary(Mode.PLANE, price_minor=700000)

    deals = _deals_to_send(route, [train, train_dearer, plane])

    assert deals == [train, plane]


def test_shared_ceiling_still_sends_exactly_one_deal() -> None:
    route = _route(max_price=800000)
    train = _itinerary(Mode.TRAIN, price_minor=300000)
    plane = _itinerary(Mode.PLANE, price_minor=700000)

    assert _deals_to_send(route, [train, plane]) == [train]


def test_no_qualifying_itinerary_sends_nothing() -> None:
    route = _route(max_price=100000)

    assert _deals_to_send(route, [_itinerary(Mode.PLANE, price_minor=700000)]) == []
