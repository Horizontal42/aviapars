from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from aviapars.adapters.cache import ResponseCache
from aviapars.db.repo_observations import ObservationsRepo

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "test.db")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


async def test_prune_expired_removes_only_expired_cache_rows(db: aiosqlite.Connection) -> None:
    cache = ResponseCache(db)

    await cache.set("expired-key", "src", b"old", None, ttl_s=-3600)  # already expired
    await cache.set("live-key", "src", b"fresh", None, ttl_s=3600)

    removed = await cache.prune_expired()

    assert removed == 1
    assert await cache.get("expired-key") is None
    assert await cache.get("live-key") == b"fresh"


async def test_prune_older_than_removes_only_old_observations(db: aiosqlite.Connection) -> None:
    repo = ObservationsRepo(db)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=200)).isoformat()
    recent = now.isoformat()

    await db.execute(
        """
        INSERT INTO price_observations
            (route_id, source_id, mode, depart_date, price_minor, currency, itinerary_hash,
             legs_json, observed_at, tier, confidence)
        VALUES (1, 'test', 'plane', '2026-09-18', 100000, 'RUB', 'old-hash', '[]', ?, 'confirmation', 1.0)
        """,
        (old,),
    )
    await db.execute(
        """
        INSERT INTO price_observations
            (route_id, source_id, mode, depart_date, price_minor, currency, itinerary_hash,
             legs_json, observed_at, tier, confidence)
        VALUES (1, 'test', 'plane', '2026-09-18', 100000, 'RUB', 'recent-hash', '[]', ?, 'confirmation', 1.0)
        """,
        (recent,),
    )
    await db.commit()

    removed = await repo.prune_older_than(days=90)

    assert removed == 1
    async with db.execute("SELECT itinerary_hash FROM price_observations") as cursor:
        rows = await cursor.fetchall()
    assert [r[0] for r in rows] == ["recent-hash"]
