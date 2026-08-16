from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from aviapars.db.models import User


class UsersRepo:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get_or_create(self, tg_user_id: int, *, is_admin: bool = False) -> User:
        existing = await self.get(tg_user_id)
        if existing is not None:
            return existing
        # created_at passed explicitly rather than relying on the schema's `datetime('now')`
        # default — that produces a different string format than Python's isoformat() used
        # everywhere else, which silently breaks any later comparison/parsing (see the
        # notifications_sent bug this was fixed alongside).
        await self._db.execute(
            "INSERT INTO users (tg_user_id, is_admin, created_at) VALUES (?, ?, ?)",
            (tg_user_id, int(is_admin), datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()
        user = await self.get(tg_user_id)
        assert user is not None
        return user

    async def get(self, tg_user_id: int) -> User | None:
        async with self._db.execute(
            """
            SELECT tg_user_id, tz, currency, quiet_hours_start, quiet_hours_end, is_admin, created_at
            FROM users WHERE tg_user_id = ?
            """,
            (tg_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return User(
            tg_user_id=row[0],
            tz=row[1],
            currency=row[2],
            quiet_hours_start=row[3],
            quiet_hours_end=row[4],
            is_admin=bool(row[5]),
            created_at=row[6],
        )

    async def is_admin(self, tg_user_id: int, admin_tg_id: int) -> bool:
        return tg_user_id == admin_tg_id
