from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite


class ResponseCache:
    """Thin wrapper over the source_cache table (§4). Backs the registry's
    cache-only call mode (allow_network=False, §5 rule 7) — the instant L0
    pass of /search reads only from here.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get(self, cache_key: str) -> bytes | None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            "SELECT body FROM source_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, now),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set(self, cache_key: str, source_id: str, body: bytes, content_type: str | None, ttl_s: int) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_s)
        await self._db.execute(
            """
            INSERT INTO source_cache (cache_key, source_id, body, content_type, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                body = excluded.body,
                content_type = excluded.content_type,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at
            """,
            (cache_key, source_id, body, content_type, now.isoformat(), expires_at.isoformat()),
        )
        await self._db.commit()

    async def prune_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute("DELETE FROM source_cache WHERE expires_at <= ?", (now,))
        await self._db.commit()
        return cursor.rowcount
