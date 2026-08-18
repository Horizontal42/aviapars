from __future__ import annotations

from datetime import datetime, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.combiner import Itinerary
from aviapars.telegram.format import format_deal_card

_ORIGIN = PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
_DEST = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)


def _offer(*, source_id: str, deep_link: str | None, confidence: float = 1.0) -> Offer:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    leg = Leg(
        mode=Mode.PLANE,
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
        price_minor=500000,
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
