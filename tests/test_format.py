from __future__ import annotations

from datetime import datetime, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.combiner import Itinerary
from aviapars.telegram.format import (
    MIXED_GROUP,
    describe_thresholds,
    format_deal_card,
    mode_group,
    select_top_itineraries,
)

_ORIGIN = PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
_DEST = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)


def _offer(
    *,
    source_id: str,
    deep_link: str | None,
    confidence: float = 1.0,
    mode: Mode = Mode.PLANE,
    price_minor: int = 500000,
) -> Offer:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    leg = Leg(
        mode=mode,
        from_place=_ORIGIN,
        to_place=_DEST,
        depart_utc=now,
        depart_local=now,
        carrier="Победа",
        number="515",
        arrive_utc=now,
        time_precision="exact",
    )
    return Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=deep_link,
        source_id=source_id,
        tier=Tier.CONFIRMATION,
        observed_at=now,
        source_cached_at=now,
        expires_at=None,
        confidence=confidence,
        itinerary_hash="abc123",
    )


def test_confirmed_card_shows_display_name_not_raw_slug() -> None:
    offer = _offer(source_id="pobeda_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card(
        "LED → MOW", itinerary, threshold_minor=600000, confirmed=True, source_names={"pobeda_direct": "Победа"}
    )

    assert "Победа" in card
    assert "pobeda_direct" not in card


def test_missing_source_names_falls_back_to_raw_slug() -> None:
    offer = _offer(source_id="pobeda_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "pobeda_direct" in card


def test_deep_link_line_included_and_deduped() -> None:
    link = "https://ticket.flypobeda.ru/websky/?origin-city-code%5B0%5D=LED"
    offer_a = _offer(source_id="pobeda_direct", deep_link=link)
    offer_b = _offer(source_id="pobeda_direct", deep_link=link)  # same link, must not repeat
    itinerary = Itinerary(offers=(offer_a, offer_b), price_minor=1000000, currency="RUB")

    card = format_deal_card(
        "LED → MOW", itinerary, threshold_minor=600000, confirmed=True, source_names={"pobeda_direct": "Победа"}
    )

    assert card.count(link) == 1
    assert "🔗 Купить у Победа" in card


def test_no_deep_link_line_when_offer_has_none() -> None:
    offer = _offer(source_id="pobeda_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "🔗" not in card


def test_departure_date_uses_local_not_utc() -> None:
    # regression: a Moscow (UTC+3) departure at 00:35 local is 21:35 the *previous* day in
    # UTC — depart_utc.date() used to be shown as the departure date, one day early.
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    dep_local = datetime(2026, 9, 10, 0, 35)
    dep_utc = datetime(2026, 9, 9, 21, 35, tzinfo=timezone.utc)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=_ORIGIN,
        to_place=_DEST,
        depart_utc=dep_utc,
        depart_local=dep_local,
        carrier="Победа",
        number="515",
        time_precision="exact",
    )
    offer = Offer(
        legs=(leg,),
        price_minor=500000,
        currency="RUB",
        deep_link=None,
        source_id="pobeda_direct",
        tier=Tier.CONFIRMATION,
        observed_at=now,
        source_cached_at=now,
        expires_at=None,
        confidence=1.0,
        itinerary_hash="abc123",
    )
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "10.09" in card
    assert "09.09" not in card


def _itinerary(*modes: Mode, price_minor: int) -> Itinerary:
    offers = tuple(
        _offer(source_id="src", deep_link=None, mode=m, price_minor=price_minor // len(modes)) for m in modes
    )
    return Itinerary(offers=offers, price_minor=price_minor, currency="RUB")


def test_describe_thresholds_shows_what_is_actually_in_effect() -> None:
    assert describe_thresholds(800000, "RUB", None) == "8 000 ₽"
    assert describe_thresholds(800000, "RUB", {"plane": 800000, "train": 400000}) == "✈️ 8 000 ₽ · 🚂 4 000 ₽"
    # declaration order, not dict order — the wizard asks plane first
    assert describe_thresholds(800000, "RUB", {"train": 400000, "plane": 800000}) == "✈️ 8 000 ₽ · 🚂 4 000 ₽"


def test_mode_group_files_a_mixed_hub_itinerary_separately() -> None:
    assert mode_group(_itinerary(Mode.PLANE, price_minor=100)) == "plane"
    assert mode_group(_itinerary(Mode.TRAIN, Mode.TRAIN, price_minor=100)) == "train"
    assert mode_group(_itinerary(Mode.PLANE, Mode.TRAIN, price_minor=100)) == MIXED_GROUP


def test_select_top_shows_every_mode_even_when_one_undercuts_the_other() -> None:
    # regression: the results screen was just the five cheapest overall, so a route where
    # trains undercut planes filled every slot with trains and the plane price the user
    # asked about never appeared at all.
    trains = [_itinerary(Mode.TRAIN, price_minor=p) for p in (100, 200, 300, 400, 500)]
    plane = _itinerary(Mode.PLANE, price_minor=9000)

    top = select_top_itineraries(trains + [plane], limit=5)

    assert plane in top
    assert len(top) == 5


def test_select_top_orders_by_group_then_price() -> None:
    plane_cheap = _itinerary(Mode.PLANE, price_minor=8000)
    plane_dear = _itinerary(Mode.PLANE, price_minor=9000)
    train = _itinerary(Mode.TRAIN, price_minor=100)
    mixed = _itinerary(Mode.PLANE, Mode.TRAIN, price_minor=50)

    top = select_top_itineraries([train, mixed, plane_dear, plane_cheap], limit=4)

    # planes first (mode-picker order), trains, then mixed last — price only orders
    # *within* a group, so the globally cheapest option is not necessarily line 1.
    assert top == [plane_cheap, plane_dear, train, mixed]


def test_select_top_keeps_a_single_group_in_pure_price_order() -> None:
    planes = [_itinerary(Mode.PLANE, price_minor=p) for p in (300, 100, 200)]

    assert [it.price_minor for it in select_top_itineraries(planes, limit=5)] == [100, 200, 300]


def test_unconfirmed_card_shows_source_and_age() -> None:
    offer = _offer(source_id="tutu_avia_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card(
        "LED → MOW",
        itinerary,
        threshold_minor=600000,
        confirmed=False,
        source_names={"tutu_avia_direct": "Туту.ру (самолёты)"},
    )

    assert "⚠️ Не подтверждено" in card
    assert "Туту.ру (самолёты)" in card
