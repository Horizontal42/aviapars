from __future__ import annotations

from pathlib import Path

import aiosqlite

from aviapars.db.migrate import _migration_files, migrate


async def test_migrate_twice_is_a_noop(tmp_path: Path) -> None:
    # Regression: 005 was missing its `INSERT INTO schema_migrations` line, so a
    # second run re-executed its ALTER TABLE statements and crashed on
    # "duplicate column name" — every migration must self-track or a restart
    # after a migration has already landed breaks the service.
    db_path = str(tmp_path / "migrate.db")
    await migrate(db_path)
    await migrate(db_path)

    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall("SELECT MAX(version) FROM schema_migrations")
        applied = row[0][0]

    latest = max(version for version, _ in _migration_files())
    assert applied == latest
