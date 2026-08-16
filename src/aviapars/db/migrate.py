from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiosqlite

from aviapars.config import REPO_ROOT, settings

MIGRATIONS_DIR = REPO_ROOT / "data" / "migrations"
_VERSION_RE = re.compile(r"^(\d+)_")


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        files.append((int(m.group(1)), path))
    return sorted(files, key=lambda pair: pair[0])


async def migrate(db_path: str | None = None) -> None:
    path = db_path or settings.db_path
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        has_migrations_table = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        applied = 0
        if has_migrations_table:
            rows = await db.execute_fetchall("SELECT MAX(version) FROM schema_migrations")
            applied = rows[0][0] or 0

        for version, path_ in _migration_files():
            if version <= applied:
                continue
            sql = path_.read_text(encoding="utf-8")
            await db.executescript(sql)
            print(f"applied {path_.name}")

        await db.commit()


if __name__ == "__main__":
    asyncio.run(migrate())
