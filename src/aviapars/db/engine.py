from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from aviapars.config import settings

_NORMALIZE_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_place_query(text: str) -> str:
    """Casefold + drop spaces/hyphens/punctuation so "Санкт-Петербург",
    "санкт петербург" and "saint-petersburg" all fold to the same key.
    SQLite's own LIKE is ASCII-only case-insensitive — it never folds
    Cyrillic case on its own, hence the custom `normalize` SQL function."""
    return _NORMALIZE_STRIP_RE.sub("", text.strip().lower())


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
        await db.create_function("normalize", 1, normalize_place_query, deterministic=True)
        yield db
    finally:
        await db.close()
