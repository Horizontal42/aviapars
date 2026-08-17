from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

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
        bot, NotificationsRepo(db), JobsRepo(db), DedupeGate(NotificationsRepo(db)), cooldown_h=12, daily_cap=3
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
        bot, NotificationsRepo(db), JobsRepo(db), DedupeGate(NotificationsRepo(db)), cooldown_h=12, daily_cap=3
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
        bot, NotificationsRepo(db), JobsRepo(db), DedupeGate(NotificationsRepo(db)), cooldown_h=12, daily_cap=3
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
