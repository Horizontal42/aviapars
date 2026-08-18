from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.db.models import User
from aviapars.db.repo_digests import DigestsRepo
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.domain.combiner import Itinerary
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier, digest_day
from aviapars.scheduler.tick import _run_due_digests

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> object:
        self.sent.append((chat_id, text))
        return object()


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "digest.db")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


def _user(*, tz: str = "Europe/Moscow", end: str = "08:00") -> User:
    return User(
        tg_user_id=555,
        tz=tz,
        currency="RUB",
        quiet_hours_start="23:00",
        quiet_hours_end=end,
        is_admin=False,
        created_at="",
    )


def _itinerary(price_minor: int, *, itinerary_hash: str = "h1") -> Itinerary:
    depart = datetime.now(timezone.utc) + timedelta(days=14)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=PlaceRef(place_id=1, iata="SVX", yandex_code=None, rzd_code=None, city_id=None),
        to_place=PlaceRef(place_id=2, iata="AER", yandex_code=None, rzd_code=None, city_id=None),
        depart_utc=depart,
        depart_local=depart,
    )
    offer = Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=None,
        source_id="pobeda_direct",
        tier=Tier.CONFIRMATION,
        observed_at=depart,
        source_cached_at=None,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=itinerary_hash,
    )
    return Itinerary(offers=(offer,), price_minor=price_minor, currency="RUB")


# --- digest window ------------------------------------------------------------------


def test_digest_window_runs_from_quiet_hours_end_not_midnight() -> None:
    user = _user(end="08:00")
    # 07:00 Moscow — before the window boundary, so this still belongs to *yesterday's* digest
    before = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
    assert digest_day(user, before).isoformat() == "2026-09-17"
    # 09:00 Moscow — the new window has opened
    after = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    assert digest_day(user, after).isoformat() == "2026-09-18"


def test_digest_window_is_computed_in_the_users_own_timezone() -> None:
    user = _user(tz="Asia/Novosibirsk", end="08:00")  # UTC+7
    # 2026-09-18 23:30 UTC is 2026-09-19 06:30 local — before that day's 08:00 boundary,
    # so it still belongs to the window that opened on the 18th.
    now = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    assert digest_day(user, now).isoformat() == "2026-09-18"


# --- accumulator --------------------------------------------------------------------


async def test_stage_keeps_the_cheapest_price_of_the_day(db: aiosqlite.Connection) -> None:
    repo = DigestsRepo(db)
    common = dict(
        currency="RUB", itinerary_hash="h", mode="plane", depart_date="2026-09-29", tier_at_send="confirmation"
    )
    assert await repo.stage(route_id=1, day="2026-09-18", price_minor=700000, card_text="dear", **common) is True
    assert await repo.stage(route_id=1, day="2026-09-18", price_minor=500000, card_text="cheap", **common) is True
    # a later, more expensive find must not displace the day's best
    assert await repo.stage(route_id=1, day="2026-09-18", price_minor=900000, card_text="dearer", **common) is False

    pending = await repo.pending()
    assert len(pending) == 1
    assert pending[0].price_minor == 500000
    assert pending[0].card_text == "cheap"


async def test_a_new_day_replaces_the_previous_best_even_if_pricier(db: aiosqlite.Connection) -> None:
    repo = DigestsRepo(db)
    common = dict(
        currency="RUB", itinerary_hash="h", mode="plane", depart_date="2026-09-29", tier_at_send="confirmation"
    )
    await repo.stage(route_id=1, day="2026-09-18", price_minor=500000, card_text="yesterday", **common)
    await repo.claim(1, "2026-09-18")
    assert await repo.stage(route_id=1, day="2026-09-19", price_minor=900000, card_text="today", **common) is True

    pending = await repo.pending()
    assert [(p.day, p.price_minor) for p in pending] == [("2026-09-19", 900000)]


async def test_claim_is_single_use_per_window(db: aiosqlite.Connection) -> None:
    """The whole double-send guard: the scheduler ticks every 60s and a closed
    window stays closed all day, so only the conditional UPDATE can decide who
    actually sends."""
    repo = DigestsRepo(db)
    await repo.stage(
        route_id=1,
        day="2026-09-18",
        price_minor=500000,
        currency="RUB",
        itinerary_hash="h",
        mode="plane",
        depart_date="2026-09-29",
        tier_at_send="confirmation",
        card_text="card",
    )
    assert await repo.claim(1, "2026-09-18") is True
    assert await repo.claim(1, "2026-09-18") is False
    assert await repo.pending() == []


# --- end to end through the scheduler ------------------------------------------------


async def _setup_route(db: aiosqlite.Connection, mode: str = "digest") -> tuple[User, int]:
    await db.execute(
        "INSERT INTO places (id, kind, name_ru, iata, country, is_hub_candidate) "
        "VALUES (1, 'city', 'Екатеринбург', 'SVX', 'RU', 0)"
    )
    await db.execute(
        "INSERT INTO places (id, kind, name_ru, iata, country, is_hub_candidate) "
        "VALUES (2, 'city', 'Сочи', 'AER', 'RU', 0)"
    )
    await db.commit()
    user = await UsersRepo(db).get_or_create(555)
    route = await RoutesRepo(db).create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=1,
        dest_place_id=2,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        max_price=700000,
        max_price_currency="RUB",
        notification_mode=mode,
    )
    return _user(), route.id


def _notifier(db: aiosqlite.Connection, bot: _FakeBot) -> Notifier:
    repo = NotificationsRepo(db)
    return Notifier(bot, repo, JobsRepo(db), DedupeGate(repo), DigestsRepo(db), cooldown_h=12, daily_cap=3)


async def test_digest_is_held_until_its_window_closes_then_sent_exactly_once(db: aiosqlite.Connection) -> None:
    user, route_id = await _setup_route(db)
    bot = _FakeBot()
    notifier = _notifier(db, bot)
    routes_repo, users_repo, digests_repo = RoutesRepo(db), UsersRepo(db), DigestsRepo(db)
    route = await routes_repo.get(route_id)
    assert route is not None

    # 12:00 Moscow — inside the window that opened at 08:00 today
    midday = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    await notifier.stage_digest(user=user, route=route, itinerary=_itinerary(600000), confirmed=True, now=midday)
    await notifier.stage_digest(user=user, route=route, itinerary=_itinerary(400000), confirmed=True, now=midday)

    # a tick inside the still-open window must not send anything
    await _run_due_digests(midday, routes_repo, users_repo, digests_repo, notifier)
    assert bot.sent == []

    # 08:30 Moscow next morning — the window has closed
    next_morning = datetime(2026, 9, 19, 5, 30, tzinfo=timezone.utc)
    await _run_due_digests(next_morning, routes_repo, users_repo, digests_repo, notifier)
    assert len(bot.sent) == 1
    assert "4 000 ₽" in bot.sent[0][1]  # the cheapest of the day, not the first one seen
    assert "лучшая цена за сутки" in bot.sent[0][1]

    # the tick runs every 60s and the window stays closed all day — still exactly one message
    for extra_min in (1, 60, 600):
        await _run_due_digests(
            next_morning + timedelta(minutes=extra_min), routes_repo, users_repo, digests_repo, notifier
        )
    assert len(bot.sent) == 1


async def test_digest_is_not_delivered_during_quiet_hours(db: aiosqlite.Connection) -> None:
    """Only reachable when the bot was down past the send time — a window closes
    exactly at quiet_hours_end, so a due digest is normally never inside them."""
    user, route_id = await _setup_route(db)
    bot = _FakeBot()
    notifier = _notifier(db, bot)
    routes_repo, users_repo, digests_repo = RoutesRepo(db), UsersRepo(db), DigestsRepo(db)
    route = await routes_repo.get(route_id)
    assert route is not None

    midday = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    await notifier.stage_digest(user=user, route=route, itinerary=_itinerary(400000), confirmed=True, now=midday)

    # 23:30 Moscow the same evening — window closed only in the sense that the bot missed it
    inside_quiet = datetime(2026, 9, 19, 20, 30, tzinfo=timezone.utc)
    await _run_due_digests(inside_quiet, routes_repo, users_repo, digests_repo, notifier)
    assert bot.sent == []

    # still staged, so it goes out once quiet hours end
    assert len(await digests_repo.pending()) == 1
    await _run_due_digests(
        datetime(2026, 9, 20, 5, 30, tzinfo=timezone.utc), routes_repo, users_repo, digests_repo, notifier
    )
    assert len(bot.sent) == 1


async def test_switching_a_route_back_to_instant_does_not_deliver_a_stale_digest(
    db: aiosqlite.Connection,
) -> None:
    user, route_id = await _setup_route(db)
    bot = _FakeBot()
    notifier = _notifier(db, bot)
    routes_repo, users_repo, digests_repo = RoutesRepo(db), UsersRepo(db), DigestsRepo(db)
    route = await routes_repo.get(route_id)
    assert route is not None

    midday = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    await notifier.stage_digest(user=user, route=route, itinerary=_itinerary(400000), confirmed=True, now=midday)
    await routes_repo.set_notification_mode(route_id, "instant")

    await _run_due_digests(
        datetime(2026, 9, 19, 5, 30, tzinfo=timezone.utc), routes_repo, users_repo, digests_repo, notifier
    )
    assert bot.sent == []


async def test_digest_card_says_when_the_price_was_found_not_just_now(db: aiosqlite.Connection) -> None:
    """A digest is read hours after the fact, so the instant card's "только что"
    would be wrong about both why it arrived and how fresh the price is."""
    user, route_id = await _setup_route(db)
    bot = _FakeBot()
    notifier = _notifier(db, bot)
    route = await RoutesRepo(db).get(route_id)
    assert route is not None

    midday = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 Moscow
    await notifier.stage_digest(user=user, route=route, itinerary=_itinerary(400000), confirmed=True, now=midday)

    card = (await DigestsRepo(db).pending())[0].card_text
    assert "найдено в 12:00" in card
    assert "только что" not in card
    assert card.startswith("📬")
