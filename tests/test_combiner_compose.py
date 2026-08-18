from __future__ import annotations

from datetime import datetime, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.combiner import _compose

_A = PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)
_B = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)
_C = PlaceRef(place_id=3, iata="LED", yandex_code=None, rzd_code=None, city_id=None)


def _leg(depart_utc: datetime, arrive_utc: datetime | None, *, precision: str = "exact") -> Leg:
    return Leg(
        mode=Mode.PLANE,
        from_place=_A,
        to_place=_B,
        depart_utc=depart_utc,
        depart_local=depart_utc,
        arrive_utc=arrive_utc,
        time_precision=precision,
    )


def _offer(leg: Leg, *, price: int = 100000) -> Offer:
    return Offer(
        legs=(leg,),
        price_minor=price,
        currency="RUB",
        deep_link=None,
        source_id="test",
        tier=Tier.CONFIRMATION,
        observed_at=leg.depart_utc,
        source_cached_at=leg.depart_utc,
        expires_at=None,
        confidence=1.0,
        itinerary_hash="h",
    )


def test_exact_precision_with_missing_arrive_utc_does_not_crash() -> None:
    # regression: rzd_direct/yandex_rasp/tutu_rail_direct/tutu_avia_direct can all emit
    # time_precision="exact" with arrive_utc=None when the upstream response omits an
    # arrival time — `_compose` used to `assert arrive_utc is not None` here and crash the
    # whole search for any hub itinerary touching such an offer.
    leg_a = _leg(datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc), None, precision="exact")
    leg_b = _leg(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc), datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc))

    itinerary = _compose(_offer(leg_a), _offer(leg_b), max_transfer_min=None, max_total_hours=None)

    assert itinerary.violations == ()  # unverifiable, not rejected — see _compose docstring
    assert itinerary.duration_minutes is None
    assert itinerary.price_minor == 200000


def test_both_arrive_utc_known_computes_gap_and_duration() -> None:
    leg_a = _leg(datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc))
    leg_b = _leg(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc), datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc))

    itinerary = _compose(_offer(leg_a), _offer(leg_b), max_transfer_min=None, max_total_hours=None)

    assert itinerary.violations == ()
    assert itinerary.duration_minutes == 360  # 8:00 -> 14:00


def test_last_leg_missing_arrive_utc_skips_duration_only() -> None:
    leg_a = _leg(datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc))
    leg_b = _leg(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc), None, precision="exact")

    itinerary = _compose(_offer(leg_a), _offer(leg_b), max_transfer_min=None, max_total_hours=None)

    # the transfer gap between A and B is still known and checked...
    assert itinerary.violations == ()
    # ...but total duration can't be computed without the final arrival.
    assert itinerary.duration_minutes is None
