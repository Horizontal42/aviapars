from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.domain.combiner import Itinerary
from aviapars.domain.dates import exact_date
from aviapars.domain.grouping import Match
from aviapars.telegram.format import (
    MIXED_GROUP,
    describe_age,
    describe_draft,
    describe_thresholds,
    format_deal_card,
    mode_group,
    screen_text,
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
    arrive_local: datetime | None = None,
    tier: Tier = Tier.CONFIRMATION,
    duration_minutes: int | None = None,
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
        arrive_local=arrive_local,
        time_precision="exact",
    )
    return Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=deep_link,
        source_id=source_id,
        tier=tier,
        observed_at=now,
        source_cached_at=now,
        expires_at=None,
        confidence=confidence,
        itinerary_hash="abc123",
        duration_minutes=duration_minutes,
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


def test_deep_link_is_clickable_and_deduped() -> None:
    link = "https://ticket.flypobeda.ru/websky/?origin-city-code%5B0%5D=LED"
    offer_a = _offer(source_id="pobeda_direct", deep_link=link)
    offer_b = _offer(source_id="pobeda_direct", deep_link=link)  # same link, must not repeat
    itinerary = Itinerary(offers=(offer_a, offer_b), price_minor=1000000, currency="RUB")

    card = format_deal_card(
        "LED → MOW", itinerary, threshold_minor=600000, confirmed=True, source_names={"pobeda_direct": "Победа"}
    )

    # parse_mode=HTML is the bot's global default, so the URL belongs inside an anchor
    # rather than sitting in the text as an unclickable string.
    assert card.count(f'<a href="{link}">купить у Победа</a>') == 1


def test_no_link_still_names_the_source() -> None:
    offer = _offer(source_id="pobeda_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "<a href" not in card
    assert "pobeda_direct (без ссылки)" in card


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


def test_card_labels_departure_arrival_and_duration() -> None:
    # the owner reported all three as missing from the first version: the times sat in an
    # unlabelled indented continuation line and the duration in the header next to the
    # date. Each one now says what it is, on the line the reader is already looking at.
    base = _offer(source_id="pobeda_direct", deep_link=None)
    offer = replace(
        base,
        legs=(
            replace(
                base.legs[0],
                arrive_utc=datetime(2026, 9, 1, 14, 25, tzinfo=timezone.utc),
                arrive_local=datetime(2026, 9, 1, 14, 25),
            ),
        ),
    )
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "🕑 вылет 12:00 · прибытие 14:25 · в пути 2ч 25м" in card
    assert "🎫 Победа · рейс 515" in card


def test_card_still_shows_duration_when_the_arrival_clock_is_unknown() -> None:
    # arrive_utc alone can't be turned into a local arrival: `tz` on a Leg is the
    # *departure* offset, and a leg that crosses zones lands on a different one. The
    # duration comes from the UTC stamps instead, which needs no timezone at all.
    base = _offer(source_id="pobeda_direct", deep_link=None)
    offer = replace(base, legs=(replace(base.legs[0], arrive_utc=datetime(2026, 9, 1, 13, 40, tzinfo=timezone.utc)),))
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    assert "🕑 вылет 12:00 · в пути 1ч 40м" in card
    assert "прибытие" not in card


def test_date_only_offer_still_shows_the_one_fact_it_has() -> None:
    # a month-matrix row has no clock time at all but does state a duration — the card
    # names what is missing and shows what isn't, instead of falling silent on both.
    base = _offer(source_id="travelpayouts", deep_link=None, tier=Tier.DISCOVERY, duration_minutes=125)
    offer = replace(
        base,
        legs=(replace(base.legs[0], carrier=None, number=None, arrive_utc=None, time_precision="date_only"),),
    )
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB", duration_minutes=125)

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=False)

    assert "🕑 время вылета источник не знает · в пути 2ч 05м" in card


def test_card_shows_the_layover_between_legs() -> None:
    hub = PlaceRef(place_id=3, iata="SVO", yandex_code=None, rzd_code=None, city_id=None, name_ru="Москва")
    dep_a = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    dep_b = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
    leg_a = Leg(
        mode=Mode.PLANE,
        from_place=_ORIGIN,
        to_place=hub,
        depart_utc=dep_a,
        depart_local=dep_a,
        arrive_utc=datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc),
    )
    leg_b = Leg(mode=Mode.PLANE, from_place=hub, to_place=_DEST, depart_utc=dep_b, depart_local=dep_b)
    offer_a = _offer(source_id="s1", deep_link="https://a.test")
    offer_b = _offer(source_id="s2", deep_link="https://b.test")
    itinerary = Itinerary(
        offers=(
            replace(offer_a, legs=(leg_a,)),
            replace(offer_b, legs=(leg_b,)),
        ),
        price_minor=1000000,
        currency="RUB",
    )

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=1100000, confirmed=True)

    assert "🔄 Пересадка 3ч · Москва (SVO)" in card
    assert "🕑 вылет 08:00 · в пути 1ч 30м" in card  # per-leg duration, not only a total
    assert "⏱ Всего в пути" in card
    # each leg keeps its own purchase line when the two come from different sources
    assert "Москва (SVO) → MOW: 5 000 ₽" in card


def test_layover_states_the_place_without_inventing_a_gap() -> None:
    hub = PlaceRef(place_id=3, iata="SVO", yandex_code=None, rzd_code=None, city_id=None, name_ru="Москва")
    dep = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    leg_a = Leg(mode=Mode.PLANE, from_place=_ORIGIN, to_place=hub, depart_utc=dep, depart_local=dep)  # no arrival
    leg_b = Leg(mode=Mode.PLANE, from_place=hub, to_place=_DEST, depart_utc=dep, depart_local=dep)
    base = _offer(source_id="s1", deep_link=None)
    itinerary = Itinerary(
        offers=(replace(base, legs=(leg_a,)), replace(base, legs=(leg_b,))),
        price_minor=1000000,
        currency="RUB",
    )

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=1100000, confirmed=True)

    assert "🔄 Пересадка · Москва (SVO)" in card


def _grouped(match: Match, *, alternate_price: int = 490000) -> Itinerary:
    anchor = _offer(source_id="aeroflot_direct", deep_link="https://af.test", duration_minutes=125)
    alternate = _offer(
        source_id="travelpayouts",
        deep_link="https://ts.test",
        price_minor=alternate_price,
        tier=Tier.DISCOVERY,
        duration_minutes=130,
    )
    return Itinerary(
        offers=(anchor,),
        price_minor=min(anchor.price_minor, alternate.price_minor),
        currency="RUB",
        duration_minutes=125,
        alternates=(alternate,),
        match=match,
        group_key="plane|1|2|2026-09-01",
    )


def test_grouped_card_makes_no_identity_claim_without_a_flight_number() -> None:
    card = format_deal_card(
        "LED → MOW",
        _grouped(Match.UNVERIFIED),
        threshold_minor=600000,
        confirmed=False,
        source_names={"aeroflot_direct": "Аэрофлот", "travelpayouts": "Aviasales"},
    )

    # this pair has matching durations; that used to read as «вероятно один и тот же
    # рейс» and now claims nothing, because a duration match is not identity evidence
    assert "❔ Один это рейс или разные" in card
    assert "ероятно" not in card
    # one card, both sources, cheapest first, each marked with its own tier
    assert "⚠️ 4 900 ₽ · <a href=\"https://ts.test\">купить у Aviasales</a>" in card
    assert "✅ 5 000 ₽ · <a href=\"https://af.test\">купить у Аэрофлот</a>" in card
    assert "💰 <b>4 900 ₽</b>" in card  # headline price is what the trip can be bought for


def test_matching_flight_number_is_stated_without_a_hedge() -> None:
    card = format_deal_card("LED → MOW", _grouped(Match.SAME_FLIGHT), threshold_minor=600000, confirmed=False)

    assert "🔁 Один и тот же рейс" in card
    assert "проверить нечем" not in card


def test_lone_itinerary_has_no_confidence_note_at_all() -> None:
    offer = _offer(source_id="pobeda_direct", deep_link=None)
    itinerary = Itinerary(offers=(offer,), price_minor=500000, currency="RUB")

    card = format_deal_card("LED → MOW", itinerary, threshold_minor=600000, confirmed=True)

    # nothing was grouped, so neither the claim nor the hedge belongs on the card
    assert "Один и тот же рейс" not in card
    assert "проверить нечем" not in card


def test_screen_text_skips_empty_blocks() -> None:
    assert screen_text("Заголовок", "", "тело") == "<b>Заголовок</b>\n\nтело"


def test_draft_line_grows_with_the_wizard() -> None:
    """The wizard is one message being rewritten, so each step has to carry the
    earlier answers itself — they are no longer visible above the prompt."""
    today = date(2026, 9, 1)
    assert describe_draft({}, today) == ""
    assert describe_draft({"origin_title": "Екатеринбург"}, today) == "Екатеринбург → …"

    full = describe_draft(
        {
            "origin_title": "Екатеринбург",
            "dest_title": "Сочи",
            "mode_pref": "plane",
            "date_selection": exact_date(date(2026, 9, 29)),
        },
        today,
    )
    assert full == "Екатеринбург → Сочи · ✈️ Самолёт · 29.09.2026"


def test_draft_line_ignores_junk_in_state() -> None:
    assert describe_draft({"mode_pref": "teleport", "date_selection": "вчера"}, date(2026, 9, 1)) == ""


def test_age_is_relative_and_rounded_down() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert describe_age(now - timedelta(seconds=30), now) == "только что"
    assert describe_age(now - timedelta(minutes=5, seconds=59), now) == "5 мин назад"
    assert describe_age(now - timedelta(hours=2), now) == "2 ч назад"
    assert describe_age(now - timedelta(days=3, hours=5), now) == "3 дн. назад"
    # A source_health row written a hair in the future (clock skew) must not read
    # as "-1 мин назад".
    assert describe_age(now + timedelta(minutes=1), now) == "только что"
