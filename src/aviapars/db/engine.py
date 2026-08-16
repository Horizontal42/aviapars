from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from aviapars.config import settings


@asynccontextmanager
async def connect(db_path: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    path = db_path or settings.db_path
    db = await aiosqlite.connect(path)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA cache_size=-8000")  # ~8MB page cache, see plans §3
        yield db
    finally:
        await db.close()
