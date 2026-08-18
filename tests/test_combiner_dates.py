from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.combiner import _dates_composable

_A = PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None)
_B = PlaceRef(place_id=2, iata="AER", yandex_code=None, rzd_code=None, city_id=None)


def _offer(day: date) -> Offer:
    depart = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=_A,
        to_place=_B,
        depart_utc=depart,
        depart_local=depart,
        time_precision="date_only",
    )
    return Offer(
        legs=(leg,),
        price_minor=100000,
        currency="RUB",
        deep_link=None,
        source_id="test",
        tier=Tier.DISCOVERY,
        observed_at=depart,
        source_cached_at=depart,
        expires_at=None,
        confidence=0.5,
        itinerary_hash=f"h{day.isoformat()}",
    )


def test_same_day_legs_compose() -> None:
    day = date(2026, 9, 10)
    assert _dates_composable(_offer(day), _offer(day))


def test_overnight_transfer_composes() -> None:
    assert _dates_composable(_offer(date(2026, 9, 10)), _offer(date(2026, 9, 11)))


def test_second_leg_before_first_never_composes() -> None:
    assert not _dates_composable(_offer(date(2026, 9, 11)), _offer(date(2026, 9, 10)))


def test_same_local_day_composes_despite_utc_date_shift() -> None:
    # regression: _dates_composable used to compare depart_utc.date() — a Moscow (UTC+3)
    # 00:35 departure is 21:35 the *previous* day in UTC, which could shift a legitimately
    # same-local-day pairing enough to fail the composability window and get dropped.
    def offer_at(dep_local: datetime, dep_utc: datetime) -> Offer:
        leg = Leg(mode=Mode.PLANE, from_place=_A, to_place=_B, depart_utc=dep_utc, depart_local=dep_local)
        return Offer(
            legs=(leg,),
            price_minor=100000,
            currency="RUB",
            deep_link=None,
            source_id="test",
            tier=Tier.DISCOVERY,
            observed_at=dep_utc,
            source_cached_at=dep_utc,
            expires_at=None,
            confidence=0.5,
            itinerary_hash="h",
        )

    leg_a = offer_at(datetime(2026, 9, 10, 0, 35), datetime(2026, 9, 9, 21, 35, tzinfo=timezone.utc))
    leg_b = offer_at(datetime(2026, 9, 10, 14, 0), datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc))

    assert _dates_composable(leg_a, leg_b)


def test_legs_days_apart_never_compose() -> None:
    """The failure a flexible window makes reachable: both leg pools span the
    whole window, so without this rule a flight on the 10th would be glued to
    a train on the 20th and sold as one itinerary. `date_only` discovery
    offers carry a placeholder midnight, so the transfer-gap check in
    `_compose` deliberately doesn't fire and can't catch it."""
    base = date(2026, 9, 10)
    for gap in (2, 5, 10):
        assert not _dates_composable(_offer(base), _offer(base + timedelta(days=gap)))
