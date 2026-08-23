from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from aiogram.exceptions import TelegramRetryAfter

from aviapars.adapters.base import Leg, Mode, Offer, PlaceRef, Tier
from aviapars.db.models import Route, User
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.domain.combiner import Itinerary
from aviapars.domain.grouping import Match
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier, _local_day_start_utc, is_quiet_hours, next_quiet_hours_end

JOBS_TABLE = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    route_id INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
"""


def _route(*, route_id: int = 1, notification_mode: str = "instant") -> Route:
    return Route(
        id=route_id,
        user_id=1,
        title="LED -> MOW",
        origin_place_id=1,
        dest_place_id=2,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-18"],
        window_start_date=None,
        window_start_offset_days=None,
        window_length_days=None,
        date_cursor=0,
        max_price=600000,
        max_price_currency="RUB",
        max_price_by_mode=None,
        pax=1,
        allow_hub_routing=False,
        hub_place_ids=None,
        max_hops=0,
        max_transfer_min=None,
        max_total_hours=None,
        poll_interval_min=60,
        notification_mode=notification_mode,
        active=True,
        next_run_at=None,
        last_run_at=None,
        created_at="",
    )


def _itinerary(*, price_minor: int = 500000, itinerary_hash: str = "h1") -> Itinerary:
    place_a = PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
    place_b = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)
    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    leg = Leg(mode=Mode.PLANE, from_place=place_a, to_place=place_b, depart_utc=now, depart_local=now)
    offer = Offer(
        legs=(leg,),
        price_minor=price_minor,
        currency="RUB",
        deep_link=None,
        source_id="pobeda_direct",
        tier=Tier.CONFIRMATION,
        observed_at=now,
        source_cached_at=now,
        expires_at=None,
        confidence=1.0,
        itinerary_hash=itinerary_hash,
    )
    return Itinerary(offers=(offer,), price_minor=price_minor, currency="RUB")


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> object:
        self.sent.append((chat_id, text))
        return object()


class _FloodOnceBot(_FakeBot):
    """Raises TelegramRetryAfter on the first send only — mimics Telegram's per-chat flood
    control right after a burst of earlier deal notifications into the same chat."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    async def send_message(self, chat_id: int, text: str) -> object:
        if not self._raised:
            self._raised = True
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)  # type: ignore[arg-type]
        return await super().send_message(chat_id, text)

MIGRATION_TABLES = """
CREATE TABLE notifications_sent (
    id INTEGER PRIMARY KEY,
    route_id INTEGER NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    itinerary_hash TEXT NOT NULL,
    price_minor INTEGER NOT NULL,
    mode TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    tier_at_send TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _user(*, tz: str = "Europe/Moscow", start: str = "23:00", end: str = "08:00") -> User:
    return User(
        tg_user_id=1,
        tz=tz,
        currency="RUB",
        quiet_hours_start=start,
        quiet_hours_end=end,
        is_admin=False,
        created_at="",
    )


def test_quiet_hours_wraps_midnight() -> None:
    user = _user()
    # 2026-08-17 02:00 Moscow (UTC+3) == 2026-08-16 23:00 UTC — well inside 23:00-08:00
    late_night_utc = datetime(2026, 8, 16, 23, 30, tzinfo=timezone.utc)
    assert is_quiet_hours(user, late_night_utc) is True

    # 2026-08-17 12:00 Moscow == 09:00 UTC — well outside the window
    midday_utc = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(user, midday_utc) is False


def test_quiet_hours_disabled_when_start_equals_end() -> None:
    user = _user(start="00:00", end="00:00")
    for hour in (0, 6, 12, 18, 23):
        now = datetime(2026, 8, 17, hour, tzinfo=timezone.utc)
        assert is_quiet_hours(user, now) is False


def test_next_quiet_hours_end_rolls_to_tomorrow_if_already_past() -> None:
    user = _user()
    # 10:00 Moscow — quiet hours end (08:00) already passed today, so next
    # end must be tomorrow morning, not today.
    now_utc = datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc)  # 10:00 Moscow
    end = next_quiet_hours_end(user, now_utc)
    assert end.astimezone(ZoneInfo(user.tz)).date().day == 18
    assert end > now_utc


@pytest.fixture
async def db(tmp_path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "notif.db")
    await conn.executescript(MIGRATION_TABLES)
    await conn.executescript(JOBS_TABLE)
    yield conn
    await conn.close()


def test_local_day_start_utc_crosses_utc_date_boundary() -> None:
    user = _user(tz="Asia/Novosibirsk")  # UTC+7
    # 2026-09-18 23:30 UTC is already 2026-09-19 local — the local day started at
    # 2026-09-18 17:00 UTC (00:00 Novosibirsk), not at 2026-09-18 00:00 UTC.
    now_utc = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    start = _local_day_start_utc(user, now_utc)
    assert start == datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc)


async def test_dedupe_gate_blocks_within_cooldown(db: aiosqlite.Connection) -> None:
    repo = NotificationsRepo(db)
    gate = DedupeGate(repo)
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    assert (
        await gate.can_send(
            route_id=1, itinerary_hash="abc", now=now, cooldown_h=12, daily_cap=3, day_start_utc=day_start
        )
        is True
    )

    await repo.record(
        route_id=1,
        dedupe_key="k1",
        itinerary_hash="abc",
        price_minor=100000,
        mode="plane",
        depart_date="2026-09-18",
        tier_at_send="discovery",
    )

    assert (
        await gate.can_send(
            route_id=1, itinerary_hash="abc", now=now, cooldown_h=12, daily_cap=3, day_start_utc=day_start
        )
        is False
    )


async def test_dedupe_gate_enforces_daily_cap(db: aiosqlite.Connection) -> None:
    repo = NotificationsRepo(db)
    gate = DedupeGate(repo)
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for i in range(3):
        await repo.record(
            route_id=2,
            dedupe_key=f"k{i}",
            itinerary_hash=f"hash{i}",
            price_minor=100000,
            mode="plane",
            depart_date="2026-09-18",
            tier_at_send="discovery",
        )

    assert (
        await gate.can_send(
            route_id=2, itinerary_hash="brand-new", now=now, cooldown_h=12, daily_cap=3, day_start_utc=day_start
        )
        is False
    )


async def test_dedupe_gate_daily_cap_uses_user_local_day_not_utc(db: aiosqlite.Connection) -> None:
    # regression: count_today_for_route used to hardcode a UTC midnight boundary, so a
    # user east of Moscow saw their daily cap reset mid-afternoon local time instead of
    # at their own midnight (and a user west of it, mid-evening the day before).
    repo = NotificationsRepo(db)
    gate = DedupeGate(repo)

    # 2026-09-18 23:30 UTC == 2026-09-19 06:30 Novosibirsk (UTC+7) — local midnight for
    # that day fell at 2026-09-18 17:00 UTC. A notification sent at 10:00 UTC the same
    # UTC calendar day is still 2026-09-18 local (17:00 Novosibirsk) — yesterday, from
    # this user's perspective — so it must not count toward *this* local day's cap.
    earlier_utc = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    await repo.record(
        route_id=3,
        dedupe_key="k-early",
        itinerary_hash="hash-early",
        price_minor=100000,
        mode="plane",
        depart_date="2026-09-18",
        tier_at_send="discovery",
    )
    # repo.record() stamps sent_at from the real wall clock, so patch it to the intended
    # UTC instant rather than asserting against whatever "now" happened to be.
    await db.execute("UPDATE notifications_sent SET sent_at = ? WHERE dedupe_key = 'k-early'", (earlier_utc.isoformat(),))
    await db.commit()

    now_utc = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    local = now_utc.astimezone(ZoneInfo("Asia/Novosibirsk"))
    day_start_utc = datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo).astimezone(timezone.utc)

    assert (
        await gate.can_send(
            route_id=3, itinerary_hash="new-one", now=now_utc, cooldown_h=12, daily_cap=1, day_start_utc=day_start_utc
        )
        is True
    )  # yesterday's (local) notification must not count against today's cap


async def test_quiet_hours_defer_records_immediately_so_dedupe_sees_it(db: aiosqlite.Connection) -> None:
    # regression: the notifications_sent row used to be written only at delivery time
    # (deliver_pending), not at enqueue time — so during quiet hours, can_send() had
    # nothing to read and the same deal was re-enqueued on every poll cycle all night.
    #
    # repo.record() stamps sent_at from the real wall clock (see repo_notifications.py),
    # so `now` here has to track real time too — a fictional pinned date would make the
    # cooldown comparison (now - real_sent_at) meaningless. An always-quiet window
    # (00:00-23:59) sidesteps needing to land `now` inside a specific quiet-hours slice;
    # that boundary logic is already covered by test_quiet_hours_wraps_midnight.
    repo = NotificationsRepo(db)
    jobs = JobsRepo(db)
    gate = DedupeGate(repo)
    bot = _FakeBot()
    notifier = Notifier(bot, repo, jobs, gate, cooldown_h=12, daily_cap=3)

    user = _user(start="00:00", end="23:59")
    route = _route()
    itinerary = _itinerary()
    now = datetime.now(timezone.utc)

    first = await notifier.send_deal(user=user, route=route, itinerary=itinerary, confirmed=True, now=now)
    assert first is False  # deferred, not sent
    assert bot.sent == []

    # a second poll cycle moments later finds the identical price again
    second = await notifier.send_deal(user=user, route=route, itinerary=itinerary, confirmed=True, now=now)
    assert second is False
    assert bot.sent == []

    pending = await jobs.due(now=next_quiet_hours_end(user, now) + timedelta(minutes=1), limit=10)
    assert len(pending) == 1  # only one job queued, not two, despite two send_deal calls


async def test_deliver_pending_does_not_double_record(db: aiosqlite.Connection) -> None:
    # regression: deliver_pending used to call repo.record() again with the same
    # dedupe_key already written at enqueue time — a UNIQUE constraint violation that
    # would crash the scheduler's notify-job delivery instead of just sending the message.
    repo = NotificationsRepo(db)
    jobs = JobsRepo(db)
    gate = DedupeGate(repo)
    bot = _FakeBot()
    notifier = Notifier(bot, repo, jobs, gate, cooldown_h=12, daily_cap=3)

    user = _user(start="00:00", end="23:59")
    route = _route()
    itinerary = _itinerary()
    now = datetime.now(timezone.utc)

    await notifier.send_deal(user=user, route=route, itinerary=itinerary, confirmed=True, now=now)
    pending = await jobs.due(now=next_quiet_hours_end(user, now) + timedelta(minutes=1), limit=10)
    assert len(pending) == 1

    await notifier.deliver_pending(pending[0])  # must not raise

    assert bot.sent == [(user.tg_user_id, bot.sent[0][1])]


async def test_digest_mode_defers_every_qualifying_deal_not_just_the_cheapest(db: aiosqlite.Connection) -> None:
    # A digest route must behave like instant mode in every way except delivery timing: two
    # distinct qualifying prices found the same day are two notifications, not one collapsed
    # "best of the day" — this is *not* delivered outside quiet hours by design.
    repo = NotificationsRepo(db)
    jobs = JobsRepo(db)
    gate = DedupeGate(repo)
    bot = _FakeBot()
    notifier = Notifier(bot, repo, jobs, gate, cooldown_h=12, daily_cap=3)

    user = _user(start="23:00", end="08:00")
    route = _route(notification_mode="digest")
    now = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 Moscow — well outside quiet hours

    first = await notifier.send_deal(
        user=user, route=route, itinerary=_itinerary(price_minor=550000, itinerary_hash="h1"), confirmed=True, now=now
    )
    second = await notifier.send_deal(
        user=user, route=route, itinerary=_itinerary(price_minor=400000, itinerary_hash="h2"), confirmed=True, now=now
    )
    assert (first, second) == (False, False)  # neither sent immediately, despite not being quiet hours
    assert bot.sent == []

    due_at = next_quiet_hours_end(user, now) + timedelta(minutes=1)
    pending = await jobs.due(now=due_at, limit=10)
    assert len(pending) == 2  # both deals queued, neither dropped in favour of the cheaper one

    for job in pending:
        await notifier.deliver_pending(job)
    assert len(bot.sent) == 2
    delivered_text = "\n".join(text for _, text in bot.sent)
    assert "5 500 ₽" in delivered_text
    assert "4 000 ₽" in delivered_text


async def test_instant_mode_still_sends_immediately_outside_quiet_hours(db: aiosqlite.Connection) -> None:
    # regression guard for the fix above: making digest mode defer unconditionally must not
    # accidentally make instant mode defer too.
    repo = NotificationsRepo(db)
    jobs = JobsRepo(db)
    gate = DedupeGate(repo)
    bot = _FakeBot()
    notifier = Notifier(bot, repo, jobs, gate, cooldown_h=12, daily_cap=3)

    user = _user(start="23:00", end="08:00")
    route = _route(notification_mode="instant")
    now = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 Moscow

    sent = await notifier.send_deal(user=user, route=route, itinerary=_itinerary(), confirmed=True, now=now)
    assert sent is True
    assert len(bot.sent) == 1


async def test_send_deal_retries_once_after_flood_control(db: aiosqlite.Connection) -> None:
    # TelegramRetryAfter is a sibling of TelegramBadRequest, not a subclass — right after a
    # burst of earlier deal notifications into the same chat, the very next send can trip
    # Telegram's per-chat flood window. Without a retry, this deal was silently dropped: the
    # exception propagated past send_deal, the scheduler tick's outer except swallowed it and
    # logged, and record() below never ran — so the deal wasn't even queued for a later poll.
    repo = NotificationsRepo(db)
    jobs = JobsRepo(db)
    gate = DedupeGate(repo)
    bot = _FloodOnceBot()
    notifier = Notifier(bot, repo, jobs, gate, cooldown_h=12, daily_cap=3)

    user = _user(start="23:00", end="08:00")
    route = _route(notification_mode="instant")
    now = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)  # 12:00 Moscow, outside quiet hours

    sent = await notifier.send_deal(user=user, route=route, itinerary=_itinerary(), confirmed=True, now=now)
    assert sent is True
    assert len(bot.sent) == 1


def _grouped_itinerary(*, with_alternate: bool, price_minor: int = 500000) -> Itinerary:
    """What the combiner now builds for one flight priced by two sources: Aeroflot's
    offer as the rendered leg, Travelpayouts' as an alternate, one shared group key."""
    place_a = PlaceRef(place_id=1, iata="LED", yandex_code=None, rzd_code=None, city_id=None)
    place_b = PlaceRef(place_id=2, iata="MOW", yandex_code=None, rzd_code=None, city_id=None)
    now = datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc)
    leg = Leg(
        mode=Mode.PLANE,
        from_place=place_a,
        to_place=place_b,
        depart_utc=now,
        depart_local=now,
        carrier="Аэрофлот",
        number="6321",
        arrive_utc=now + timedelta(minutes=125),
    )

    def offer(source_id: str, tier: Tier, price: int) -> Offer:
        return Offer(
            legs=(leg,),
            price_minor=price,
            currency="RUB",
            deep_link=None,
            source_id=source_id,
            tier=tier,
            observed_at=now,
            source_cached_at=now,
            expires_at=None,
            confidence=1.0,
            itinerary_hash=f"{source_id}-hash",
            duration_minutes=125,
        )

    anchor = offer("aeroflot_direct", Tier.CONFIRMATION, price_minor + 70000)
    alternates = (offer("travelpayouts", Tier.DISCOVERY, price_minor),) if with_alternate else ()
    return Itinerary(
        offers=(anchor,),
        price_minor=min([anchor.price_minor, *(a.price_minor for a in alternates)]),
        currency="RUB",
        duration_minutes=125,
        alternates=alternates,
        match=Match.LIKELY_SAME if alternates else None,
        group_key="plane|1|2|2026-09-18|6321",
    )


async def test_grouped_deal_is_one_notification_carrying_every_source(db: aiosqlite.Connection) -> None:
    repo = NotificationsRepo(db)
    notifier = Notifier(
        _FakeBot(),
        repo,
        JobsRepo(db),
        DedupeGate(repo),
        cooldown_h=12,
        daily_cap=3,
        source_names={"aeroflot_direct": "Аэрофлот", "travelpayouts": "Aviasales"},
    )
    bot = notifier._bot
    user = _user(start="23:00", end="08:00")
    now = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)

    sent = await notifier.send_deal(
        user=user, route=_route(), itinerary=_grouped_itinerary(with_alternate=True), confirmed=False, now=now
    )

    assert sent is True
    assert len(bot.sent) == 1  # one message for what used to be two
    text = bot.sent[0][1]
    assert "Аэрофлот" in text and "Aviasales" in text
    assert "Вероятно один и тот же рейс" in text


async def test_cooldown_keys_on_the_trip_not_on_which_sources_reported_it(db: aiosqlite.Connection) -> None:
    # The whole point of grouping: the next cycle finding only Aeroflot (Travelpayouts
    # timed out, or its row aged out) is the same trip, not a new deal to announce.
    repo = NotificationsRepo(db)
    notifier = Notifier(_FakeBot(), repo, JobsRepo(db), DedupeGate(repo), cooldown_h=12, daily_cap=3)
    bot = notifier._bot
    # quiet hours disabled (start == end) so `now` can be the real clock, which is what
    # the cooldown compares against — notifications_sent stamps rows with datetime.now().
    user = _user(start="00:00", end="00:00")
    route = _route()
    now = datetime.now(timezone.utc)

    first = await notifier.send_deal(
        user=user, route=route, itinerary=_grouped_itinerary(with_alternate=True), confirmed=False, now=now
    )
    second = await notifier.send_deal(
        user=user,
        route=route,
        itinerary=_grouped_itinerary(with_alternate=False),
        confirmed=True,
        now=now + timedelta(hours=2),
    )

    assert first is True
    assert second is False
    assert len(bot.sent) == 1


async def test_a_different_flight_on_the_same_day_still_notifies(db: aiosqlite.Connection) -> None:
    # Suppression is per trip, not per date — a genuinely different flight found later
    # the same day must still get through the cooldown gate.
    repo = NotificationsRepo(db)
    notifier = Notifier(_FakeBot(), repo, JobsRepo(db), DedupeGate(repo), cooldown_h=12, daily_cap=3)
    bot = notifier._bot
    user = _user(start="00:00", end="00:00")
    route = _route()
    now = datetime.now(timezone.utc)

    other_flight = replace(_grouped_itinerary(with_alternate=False), group_key="plane|1|2|2026-09-18|6777")

    await notifier.send_deal(
        user=user, route=route, itinerary=_grouped_itinerary(with_alternate=False), confirmed=True, now=now
    )
    sent = await notifier.send_deal(
        user=user, route=route, itinerary=other_flight, confirmed=True, now=now + timedelta(hours=1)
    )

    assert sent is True
    assert len(bot.sent) == 2
