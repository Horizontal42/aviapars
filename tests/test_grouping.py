from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.grouping import Match, group_offers

_PEZ = PlaceRef(place_id=1, iata="PEZ", yandex_code=None, rzd_code=None, city_id=None, name_ru="Пенза")
_LED = PlaceRef(place_id=2, iata="LED", yandex_code=None, rzd_code=None, city_id=None, name_ru="Санкт-Петербург")
_SVO = PlaceRef(place_id=3, iata="SVO", yandex_code=None, rzd_code=None, city_id=None, name_ru="Москва")

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _confirmation(
    *,
    source_id: str = "aeroflot_direct",
    carrier: str | None = "Аэрофлот",
    number: str | None = "6321",
    depart: datetime = datetime(2026, 9, 14, 7, 35),
    duration_minutes: int | None = 125,
    price_minor: int = 619000,
    stops_via: PlaceRef | None = None,
) -> Offer:
    """A carrier's own offer: real times, carrier and flight number."""
    legs: list[Leg] = []
    hops = [(_PEZ, stops_via), (stops_via, _LED)] if stops_via else [(_PEZ, _LED)]
    for i, (frm, to) in enumerate(hops):
        assert frm is not None and to is not None
        start = depart + timedelta(hours=3 * i)
        legs.append(
            Leg(
                mode=Mode.PLANE,
                from_place=frm,
                to_place=to,
                depart_utc=start.replace(tzinfo=timezone.utc),
                depart_local=start,
                carrier=carrier,
                number=f"{number}{i}" if (number and stops_via) else number,
                arrive_utc=(start + timedelta(minutes=120)).replace(tzinfo=timezone.utc),
                arrive_local=start + timedelta(minutes=120),
            )
        )
    return Offer(
        legs=tuple(legs),
        price_minor=price_minor,
        currency="RUB",
        deep_link=f"https://example.test/{source_id}",
        source_id=source_id,
        tier=Tier.CONFIRMATION,
        observed_at=_NOW,
        source_cached_at=_NOW,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=f"{source_id}-hash",
        duration_minutes=duration_minutes,
    )


def _discovery(
    *,
    source_id: str = "travelpayouts",
    day: date = date(2026, 9, 14),
    duration_minutes: int | None = 130,
    price_minor: int = 549000,
) -> Offer:
    """A month-matrix style offer: date, price, duration — no carrier, no flight number."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=_PEZ,
        to_place=_LED,
        depart_utc=midnight,
        depart_local=midnight,
        time_precision="date_only",
    )
    return Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link="https://example.test/aviasales",
        source_id=source_id,
        tier=Tier.DISCOVERY,
        observed_at=_NOW,
        source_cached_at=_NOW,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=f"{source_id}-hash",
        duration_minutes=duration_minutes,
    )


def test_a_number_less_offer_groups_without_any_same_flight_claim() -> None:
    groups = group_offers([_discovery(duration_minutes=130), _confirmation(duration_minutes=125)])

    assert len(groups) == 1
    group = groups[0]
    # matching durations used to read as "вероятно один и тот же рейс"; they are not
    # evidence of identity at all — see test_near_identical_durations_prove_nothing
    assert group.match is Match.UNVERIFIED
    assert group.anchor.source_id == "aeroflot_direct"  # the side that knows the flight number
    assert [o.source_id for o in group.alternates] == ["travelpayouts"]
    assert group.price_minor == 549000  # cheapest member — what the trip actually costs
    assert group.duration_minutes == 125


def test_near_identical_durations_prove_nothing() -> None:
    """Live numbers from the owner's route (PEZ→LED, 2026-08-24): every direct offer from
    every source came back at 130 minutes. Two genuinely different same-day departures
    match just as exactly as one flight seen twice, so a duration match must never be
    dressed up as probable identity."""
    morning = _confirmation(source_id="tutu_avia_direct", number="6768", depart=datetime(2026, 9, 14, 8, 0))
    aggregator_row = _discovery(duration_minutes=130)

    groups = group_offers([morning, aggregator_row])

    assert len(groups) == 1
    assert groups[0].match is Match.UNVERIFIED
    assert groups[0].match is not None  # still grouped: one card per route+date, just no claim


def test_clearly_different_durations_do_not_group() -> None:
    # 2ч05м direct against 6ч via a hub: the durations themselves are the evidence that
    # these are two different trips, not a missing signal.
    groups = group_offers([_discovery(duration_minutes=360), _confirmation(duration_minutes=125)])

    assert len(groups) == 2
    assert all(g.match is None and not g.alternates for g in groups)


def test_matching_flight_numbers_group_without_hedging() -> None:
    a = _confirmation(source_id="aeroflot_direct", carrier="Аэрофлот", number="SU 6321")
    b = _confirmation(source_id="tutu_avia_direct", carrier="Аэрофлот - российские авиалинии", number="su6321")

    groups = group_offers([a, b])

    assert len(groups) == 1
    assert groups[0].match is Match.SAME_FLIGHT


def test_different_flight_numbers_never_group() -> None:
    a = _confirmation(source_id="aeroflot_direct", number="6321")
    b = _confirmation(source_id="tutu_avia_direct", number="6777")

    assert len(group_offers([a, b])) == 2


def test_different_carriers_never_group_even_when_durations_agree() -> None:
    a = _confirmation(source_id="aeroflot_direct", carrier="Аэрофлот", number=None, duration_minutes=125)
    b = _confirmation(source_id="azimuth_direct", carrier="Азимут", number=None, duration_minutes=127)

    assert len(group_offers([a, b])) == 2


def test_a_duration_derived_from_timestamps_still_disqualifies() -> None:
    # azimuth/pobeda fill arrival timestamps but never duration_minutes; the conflict
    # check derives it rather than treating those offers as "no duration known".
    slow = replace(
        _confirmation(source_id="azimuth_direct", number=None, duration_minutes=None),
        legs=(
            replace(
                _confirmation(number=None).legs[0],
                carrier=None,
                number=None,
                arrive_utc=datetime(2026, 9, 14, 15, 35, tzinfo=timezone.utc),  # 8h after departure
            ),
        ),
    )

    assert len(group_offers([slow, _discovery(duration_minutes=130)])) == 2


def test_no_corroborating_signal_groups_but_claims_nothing() -> None:
    # An aggregator row with no duration either: same route and date is all we have, and
    # the group says exactly that instead of asserting a shared flight.
    groups = group_offers([_discovery(duration_minutes=None), _confirmation(duration_minutes=125)])

    assert len(groups) == 1
    assert groups[0].match is Match.UNVERIFIED


def test_two_offers_from_one_source_stay_separate() -> None:
    morning = _confirmation(number="6321", depart=datetime(2026, 9, 14, 7, 35))
    evening = _confirmation(number="6323", depart=datetime(2026, 9, 14, 19, 5))

    groups = group_offers([morning, evening])

    assert len(groups) == 2  # the morning and the evening flight, not two reports of one


def test_different_dates_and_stop_counts_stay_separate() -> None:
    direct = _confirmation(number=None, duration_minutes=125)
    via_hub = _confirmation(source_id="tutu_avia_direct", number=None, duration_minutes=125, stops_via=_SVO)
    other_day = _discovery(day=date(2026, 9, 15), duration_minutes=125)

    assert len(group_offers([direct, via_hub, other_day])) == 3


def test_group_key_is_the_route_and_day_not_the_sources_that_reported_it() -> None:
    """Cooldown identity has to survive both a source dropping out and the same trip
    being seen by different tiers in different cycles — per-date confirmation adapters
    probe a rotating slice of a wide window while batch sources see all of it, so the
    aggregator's row and the carrier's price for one date routinely never meet in a
    single search."""
    both = group_offers([_discovery(), _confirmation()])
    aggregator_only = group_offers([_discovery()])
    carrier_only = group_offers([_confirmation()])
    other_day = group_offers([_discovery(day=date(2026, 9, 15))])

    assert both[0].key == aggregator_only[0].key == carrier_only[0].key
    assert other_day[0].key != both[0].key


def test_duration_disqualifies_only_past_measurement_noise() -> None:
    # 20 minutes apart on a 2ч flight is how differently two sources measure one trip;
    # 3ч apart is a different routing. The window scales so a long trip may differ by
    # proportionally more before it counts as a different trip.
    assert len(group_offers([_discovery(duration_minutes=145), _confirmation(duration_minutes=125)])) == 1
    assert len(group_offers([_discovery(duration_minutes=305), _confirmation(duration_minutes=125)])) == 2
    assert len(group_offers([_discovery(duration_minutes=660), _confirmation(duration_minutes=600)])) == 1
