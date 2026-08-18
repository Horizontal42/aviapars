from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from aviapars.db.repo_routes import RoutesRepo

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "routes.db")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    await conn.execute("INSERT INTO users (tg_user_id, tz, currency) VALUES (1, 'Europe/Moscow', 'RUB')")
    await conn.execute(
        "INSERT INTO places (id, kind, name_ru, country) VALUES (1, 'city', 'Пенза', 'RU'), (2, 'city', 'Сочи', 'RU')"
    )
    await conn.commit()
    yield conn
    await conn.close()


async def _create(db: aiosqlite.Connection, by_mode: dict[str, int] | None) -> int:
    route = await RoutesRepo(db).create(
        user_id=1,
        title="Пенза → Сочи",
        origin_place_id=1,
        dest_place_id=2,
        mode_pref="auto",
        date_mode="fixed",
        fixed_dates=["2026-09-10"],
        max_price=800000,
        max_price_currency="RUB",
        max_price_by_mode=by_mode,
    )
    return route.id


async def test_per_mode_thresholds_round_trip(db: aiosqlite.Connection) -> None:
    # the JSON column landed between max_price_currency and pax, which shifts every
    # positional index in _row_to_route — a round-trip is what catches getting that wrong.
    route_id = await _create(db, {"plane": 800000, "train": 400000})

    route = await RoutesRepo(db).get(route_id)

    assert route is not None
    assert route.max_price_by_mode == {"plane": 800000, "train": 400000}
    assert route.max_price == 800000
    assert route.poll_interval_min == 60
    assert route.notification_mode == "instant"
    assert route.active is True


async def test_shared_threshold_stores_null_not_an_empty_object(db: aiosqlite.Connection) -> None:
    route_id = await _create(db, None)

    route = await RoutesRepo(db).get(route_id)

    assert route is not None
    assert route.max_price_by_mode is None


async def test_set_poll_interval_updates_only_that_route(db: aiosqlite.Connection) -> None:
    repo = RoutesRepo(db)
    route_id = await _create(db, None)
    other_id = await _create(db, None)

    await repo.set_poll_interval(route_id, 15)

    updated = await repo.get(route_id)
    other = await repo.get(other_id)
    assert updated is not None and updated.poll_interval_min == 15
    assert other is not None and other.poll_interval_min == 60  # untouched
