from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from aviapars.db.repo_jobs import JobsRepo

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(tmp_path / "test.db")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


async def test_reset_stuck_running_requeues_only_running_jobs(db: aiosqlite.Connection) -> None:
    # regression: a job that was mark_running'd and then never reached mark_done/
    # mark_failed (process killed mid-delivery) used to sit in 'running' forever -
    # due() only ever selects state='queued', so it was never picked up again and the
    # deferred notification it represents was lost silently.
    repo = JobsRepo(db)
    now = datetime.now(timezone.utc)

    stuck_id = await repo.enqueue("notify", route_id=1, payload={"chat_id": 1, "text": "stuck"})
    queued_id = await repo.enqueue("notify", route_id=2, payload={"chat_id": 2, "text": "queued"})
    done_id = await repo.enqueue("notify", route_id=3, payload={"chat_id": 3, "text": "done"})

    await repo.mark_running(stuck_id)
    await repo.mark_running(done_id)
    await repo.mark_done(done_id)

    reset_count = await repo.reset_stuck_running()

    assert reset_count == 1
    pending = await repo.due(now=now, limit=10)
    pending_ids = {job.id for job in pending}
    assert stuck_id in pending_ids  # recovered, ready to retry
    assert queued_id in pending_ids  # untouched, was never running
    assert done_id not in pending_ids  # already delivered, must not be resurrected
