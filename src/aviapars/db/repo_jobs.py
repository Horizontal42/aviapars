from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite


@dataclass(slots=True)
class Job:
    id: int
    kind: str
    route_id: int | None
    priority: int
    payload: dict[str, Any]
    state: str
    attempts: int
    run_after: str | None


class JobsRepo:
    """Durable queue backing the scheduler (§6/§13 Phase X notify-deferral).
    Survives a restart — nothing queued is lost, unlike a pure in-memory
    asyncio.PriorityQueue would be.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def enqueue(
        self,
        kind: str,
        *,
        route_id: int | None = None,
        priority: int = 0,
        payload: dict[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> int:
        cursor = await self._db.execute(
            "INSERT INTO jobs (kind, route_id, priority, payload_json, run_after, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                route_id,
                priority,
                json.dumps(payload or {}),
                run_after.isoformat() if run_after else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def due(self, *, now: datetime, limit: int = 20) -> list[Job]:
        async with self._db.execute(
            """
            SELECT id, kind, route_id, priority, payload_json, state, attempts, run_after
            FROM jobs
            WHERE state = 'queued' AND (run_after IS NULL OR run_after <= ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (now.isoformat(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            Job(
                id=row[0],
                kind=row[1],
                route_id=row[2],
                priority=row[3],
                payload=json.loads(row[4]) if row[4] else {},
                state=row[5],
                attempts=row[6],
                run_after=row[7],
            )
            for row in rows
        ]

    async def mark_running(self, job_id: int) -> None:
        await self._db.execute(
            "UPDATE jobs SET state = 'running', attempts = attempts + 1 WHERE id = ?", (job_id,)
        )
        await self._db.commit()

    async def mark_done(self, job_id: int) -> None:
        await self._db.execute(
            "UPDATE jobs SET state = 'done', finished_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        await self._db.commit()

    async def mark_failed(self, job_id: int) -> None:
        await self._db.execute(
            "UPDATE jobs SET state = 'failed', finished_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        await self._db.commit()
