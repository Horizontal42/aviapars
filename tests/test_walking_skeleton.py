from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from aviapars.adapters.base import (
    HealthStatus,
    Leg,
    Mode,
    Offer,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
)
from aviapars.adapters.registry import AdapterRegistry
from aviapars.adapters.travelpayouts import TravelpayoutsAdapter
from aviapars.config import settings
from aviapars.db.models import User
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.worker import process_route

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"
FIXTURE = Path(__file__).parent / "fixtures" / "travelpayouts" / "month_matrix_svx_aer_2026-09.json"


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    path = tmp_path / "test.db"
    conn = await aiosqlite.connect(path)
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


async def _disable_quiet_hours(db: aiosqlite.Connection, user: User) -> User:
    """Tests must not depend on the wall-clock time they happen to run at —
    quiet hours (§9) are tested separately in test_notifier.py."""
    await db.execute(
        "UPDATE users SET quiet_hours_start = '00:00', quiet_hours_end = '00:00' WHERE tg_user_id = ?",
        (user.tg_user_id,),
    )
    await db.commit()
    return replace(user, quiet_hours_start="00:00", quiet_hours_end="00:00")


async def _seed_places(db: aiosqlite.Connection) -> tuple[int, int]:
    await db.execute(
        "INSERT INTO places (id, kind, name_ru, iata, country, is_hub_candidate) VALUES (1, 'city', 'Екатеринбург', 'SVX', 'RU', 0)"
    )
    await db.execute(
        "INSERT INTO places (id, kind, name_ru, iata, country, is_hub_candidate) VALUES (2, 'city', 'Сочи', 'AER', 'RU', 0)"
    )
    await db.commit()
    return 1, 2


async def test_cheap_direct_flight_triggers_notification(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    origin_id, dest_id = await _seed_places(db)

    users_repo = UsersRepo(db)
    user = await users_repo.get_or_create(555, is_admin=True)
    user = await _disable_quiet_hours(db, user)

    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        window_start_offset_days=None,
        window_length_days=None,
        max_price=700000,  # 7000 RUB in kopecks — the fixture's cheapest direct is 6101 RUB
        max_price_currency="RUB",
    )

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))

    bot = FakeBot()
    notifier = Notifier(
        bot,
        NotificationsRepo(db),
        JobsRepo(db),
        DedupeGate(NotificationsRepo(db)),
        cooldown_h=12,
        daily_cap=3,
    )

    await process_route(
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=notifier,
    )

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 555
    assert "6 101" in text or "6101" in text
    assert "Екатеринбург → Сочи" in text

    async with db.execute("SELECT COUNT(*) FROM price_observations WHERE route_id = ?", (route.id,)) as cur:
        row = await cur.fetchone()
    assert row[0] >= 1


async def test_route_deleted_mid_cycle_does_not_crash_the_notify_step(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: "🔍 Проверить сейчас" on a route the user deletes while a slow
    search is still in flight left `route.id` gone by the time observation-recording
    ran — an unhandled FOREIGN KEY IntegrityError that crashed the whole update and,
    since it happened before the notify step in process_route(), silently swallowed
    the notification too. Reproduced here by actually deleting the route out from
    under an in-flight call, same as the live crash."""
    await db.execute("PRAGMA foreign_keys = ON")
    origin_id, dest_id = await _seed_places(db)

    users_repo = UsersRepo(db)
    user = await users_repo.get_or_create(555, is_admin=True)
    user = await _disable_quiet_hours(db, user)

    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        window_start_offset_days=None,
        window_length_days=None,
        max_price=700000,
        max_price_currency="RUB",
    )
    await routes_repo.delete(route.id)  # gone before process_route() ever touches the DB again

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))

    bot = FakeBot()
    notifier = Notifier(bot, NotificationsRepo(db), JobsRepo(db), DedupeGate(NotificationsRepo(db)), cooldown_h=12, daily_cap=3)

    await process_route(  # must not raise
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=notifier,
    )

    async with db.execute("SELECT COUNT(*) FROM price_observations") as cur:
        row = await cur.fetchone()
    assert row[0] == 0  # the doomed insert didn't happen, but nothing else crashed either
    # the deal still reached the user — record()'s own FK failure (notify/notifier.py) is
    # caught the same way, logged, and does not stop the message that already went out
    assert len(bot.sent) == 1


async def test_digest_route_defers_instead_of_messaging_immediately(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same qualifying price as the test above, same threshold check — only the
    delivery timing differs. crosses_threshold() stays mode-agnostic;
    notifier.send_deal() defers instead of sending immediately."""
    origin_id, dest_id = await _seed_places(db)
    users_repo = UsersRepo(db)
    user = await users_repo.get_or_create(557, is_admin=True)
    user = await _disable_quiet_hours(db, user)

    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        max_price=700000,
        max_price_currency="RUB",
        notification_mode="digest",
    )

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))

    bot = FakeBot()
    notifier = Notifier(
        bot,
        NotificationsRepo(db),
        JobsRepo(db),
        DedupeGate(NotificationsRepo(db)),
        cooldown_h=12,
        daily_cap=3,
    )

    await process_route(
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=notifier,
    )

    assert bot.sent == []
    # far-future "now" so the exact next_quiet_hours_end computation doesn't matter here —
    # this test only cares that a notify job for this route was queued, not exactly when.
    far_future = datetime.now(timezone.utc) + timedelta(days=2)
    pending = await JobsRepo(db).due(now=far_future, limit=10)
    route_jobs = [j for j in pending if j.route_id == route.id and j.kind == "notify"]
    assert len(route_jobs) == 1
    assert "Екатеринбург → Сочи" in route_jobs[0].payload["text"]


async def test_price_above_threshold_does_not_notify(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    origin_id, dest_id = await _seed_places(db)
    users_repo = UsersRepo(db)
    user = await users_repo.get_or_create(556)
    user = await _disable_quiet_hours(db, user)
    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        window_start_offset_days=None,
        window_length_days=None,
        max_price=100,  # 1 RUB — nothing will ever be this cheap
        max_price_currency="RUB",
    )

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))

    bot = FakeBot()
    notifier = Notifier(
        bot,
        NotificationsRepo(db),
        JobsRepo(db),
        DedupeGate(NotificationsRepo(db)),
        cooldown_h=12,
        daily_cap=3,
    )

    await process_route(
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=notifier,
    )

    assert bot.sent == []


async def test_flexible_window_finds_a_date_the_user_never_named(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flexible route stores a window, not a date. The batch-capable source
    gets the whole window in one request (so the cheapest day in the month is
    found even though nobody asked for that specific day), while the date
    cursor moves on so one-request-per-date sources rotate through the window
    over the next few cycles instead of scanning all 30 days every time.
    """
    origin_id, dest_id = await _seed_places(db)
    users_repo = UsersRepo(db)
    user = await users_repo.get_or_create(557, is_admin=True)
    user = await _disable_quiet_hours(db, user)

    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="flexible",
        fixed_dates=None,
        window_start_date="2026-09-01",
        window_length_days=30,
        max_price=700000,
        max_price_currency="RUB",
    )
    assert route.date_cursor == 0

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))

    bot = FakeBot()
    notifier = Notifier(
        bot,
        NotificationsRepo(db),
        JobsRepo(db),
        DedupeGate(NotificationsRepo(db)),
        cooldown_h=12,
        daily_cap=3,
    )

    await process_route(
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=notifier,
    )

    assert len(bot.sent) == 1

    async with db.execute(
        "SELECT COUNT(DISTINCT depart_date) FROM price_observations WHERE route_id = ?", (route.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] > 1  # whole window priced, not just one date

    after = await routes_repo.get(route.id)
    assert after is not None
    assert after.date_cursor == settings.flex_probe_dates_per_cycle


class _FakeCarrierAdapter:
    """Stands in for a confirmation-tier source (aeroflot_direct) answering the same
    route+date as the Travelpayouts fixture: real times, carrier and flight number, and a
    duration close enough to the aggregator's to corroborate it."""

    id = "fake_carrier"
    display_name = "Авиакомпания"
    modes = frozenset({Mode.PLANE})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = None
    supports_batch_dates = True
    rate_limit = RateLimitPolicy(min_interval_s=0)
    default_cache_ttl_s = 0
    request_timeout_s = 5
    total_timeout_s = 5

    def __init__(self, *, duration_minutes: int, price_minor: int = 690000) -> None:
        self._duration_minutes = duration_minutes
        self._price_minor = price_minor

    async def search(self, q: SearchQuery) -> SearchResult:
        depart = datetime(2026, 9, 29, 7, 35, tzinfo=timezone.utc)
        leg = Leg(
            mode=Mode.PLANE,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=depart,
            depart_local=depart.replace(tzinfo=None),
            carrier="Авиакомпания",
            number="1234",
            arrive_utc=depart + timedelta(minutes=self._duration_minutes),
            arrive_local=(depart + timedelta(minutes=self._duration_minutes)).replace(tzinfo=None),
        )
        now = datetime.now(timezone.utc)
        return SearchResult(
            status=ResultStatus.OK,
            offers=[
                Offer(
                    legs=(leg,),
                    price_minor=self._price_minor,
                    currency="RUB",
                    deep_link="https://carrier.test/book",
                    source_id=self.id,
                    tier=Tier.CONFIRMATION,
                    observed_at=now,
                    source_cached_at=now,
                    expires_at=None,
                    confidence=1.0,
                    itinerary_hash="fake-carrier-hash",
                    duration_minutes=self._duration_minutes,
                )
            ],
            partial=False,
        )

    async def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True)


async def _run_two_source_cycle(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch, *, carrier_duration_minutes: int
) -> FakeBot:
    origin_id, dest_id = await _seed_places(db)
    users_repo = UsersRepo(db)
    user = await _disable_quiet_hours(db, await users_repo.get_or_create(555, is_admin=True))
    routes_repo = RoutesRepo(db)
    route = await routes_repo.create(
        user_id=user.tg_user_id,
        title="Екатеринбург → Сочи",
        origin_place_id=origin_id,
        dest_place_id=dest_id,
        mode_pref="plane",
        date_mode="fixed",
        fixed_dates=["2026-09-29"],
        window_start_offset_days=None,
        window_length_days=None,
        max_price=700000,
        max_price_currency="RUB",
    )

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def fake_fetch_month(self, session, origin, dest, month, currency):
        return payload["data"], True

    monkeypatch.setattr(TravelpayoutsAdapter, "_fetch_month", fake_fetch_month)

    registry = AdapterRegistry(
        db, http_concurrency_global=2, http_concurrency_interactive=1, http_concurrency_per_source=1
    )
    registry.register(TravelpayoutsAdapter(token="fake", marker="fake"))
    registry.register(_FakeCarrierAdapter(duration_minutes=carrier_duration_minutes))

    bot = FakeBot()
    await process_route(
        route,
        user=user,
        registry=registry,
        places_repo=PlacesRepo(db),
        routes_repo=routes_repo,
        observations_repo=ObservationsRepo(db),
        notifier=Notifier(
            bot,
            NotificationsRepo(db),
            JobsRepo(db),
            DedupeGate(NotificationsRepo(db)),
            cooldown_h=12,
            daily_cap=3,
            source_names={"travelpayouts": "Aviasales", "fake_carrier": "Авиакомпания"},
        ),
    )
    return bot


async def test_two_sources_for_one_flight_produce_a_single_notification(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's actual complaint: one Пенза→Питер trip arriving as two messages a day,
    once from the carrier and once from the aggregator. The aggregator says 4ч, the carrier
    4ч10м — close enough to corroborate, so it is one card that hedges honestly."""
    bot = await _run_two_source_cycle(db, monkeypatch, carrier_duration_minutes=250)

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Вероятно один и тот же рейс" in text
    assert "купить у Aviasales" in text and "купить у Авиакомпания" in text
    assert "6 101 ₽" in text  # cheapest of the two, and the headline price
    assert "07:35 → 11:45" in text  # the carrier's real times, not the aggregator's midnight

    # both sources' prices are still recorded — grouping is a delivery decision, not a
    # reason to lose one source's price history
    async with db.execute("SELECT DISTINCT source_id FROM price_observations") as cur:
        sources = sorted(row[0] for row in await cur.fetchall())
    assert sources == ["fake_carrier", "travelpayouts"]


async def test_durations_that_disagree_are_not_merged_into_one_flight(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 4ч against 1ч30м on the same route and date is evidence of two different trips, so
    # the cheapest one is reported on its own rather than dressed up as a two-source deal.
    bot = await _run_two_source_cycle(db, monkeypatch, carrier_duration_minutes=90)

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "рейс" not in text  # no grouping claim of any kind
    assert "купить у Авиакомпания" not in text  # cheapest qualifying deal is the aggregator's
