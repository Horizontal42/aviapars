from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from aviapars.db.models import User
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import is_quiet_hours, next_quiet_hours_end

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
    yield conn
    await conn.close()


async def test_dedupe_gate_blocks_within_cooldown(db: aiosqlite.Connection) -> None:
    repo = NotificationsRepo(db)
    gate = DedupeGate(repo)
    now = datetime.now(timezone.utc)

    assert await gate.can_send(route_id=1, itinerary_hash="abc", now=now, cooldown_h=12, daily_cap=3) is True

    await repo.record(
        route_id=1,
        dedupe_key="k1",
        itinerary_hash="abc",
        price_minor=100000,
        mode="plane",
        depart_date="2026-09-18",
        tier_at_send="discovery",
    )

    assert await gate.can_send(route_id=1, itinerary_hash="abc", now=now, cooldown_h=12, daily_cap=3) is False


async def test_dedupe_gate_enforces_daily_cap(db: aiosqlite.Connection) -> None:
    repo = NotificationsRepo(db)
    gate = DedupeGate(repo)
    now = datetime.now(timezone.utc)

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

    assert await gate.can_send(route_id=2, itinerary_hash="brand-new", now=now, cooldown_h=12, daily_cap=3) is False
