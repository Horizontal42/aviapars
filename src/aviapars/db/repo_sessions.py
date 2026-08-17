from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite


class SessionsRepo:
    """Cache for browser-solved sessions (cookies/tokens) — see migration 004
    and adapters/s7_direct.py for the cheap-path/browser-fallback rationale.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get(self, source_id: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT payload_json, expires_at FROM source_sessions WHERE source_id = ?",
            (source_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        payload_json, expires_at = row
        if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            return None
        result: dict[str, Any] = json.loads(payload_json)
        return result

    async def save(self, source_id: str, payload: dict[str, Any], expires_at: datetime) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO source_sessions (source_id, payload_json, obtained_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                obtained_at = excluded.obtained_at,
                expires_at = excluded.expires_at
            """,
            (source_id, json.dumps(payload), now, expires_at.isoformat()),
        )
        await self._db.commit()

    async def invalidate(self, source_id: str) -> None:
        await self._db.execute("DELETE FROM source_sessions WHERE source_id = ?", (source_id,))
        await self._db.commit()
