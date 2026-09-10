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
            SELECT tg_user_id, tz, currency, quiet_hours_start, quiet_hours_end, digest_time,
                   is_admin, created_at
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
            digest_time=row[5],
            is_admin=bool(row[6]),
            created_at=row[7],
        )

    async def get_multi(self, tg_user_ids: list[int]) -> dict[int, User]:
        if not tg_user_ids:
            return {}

        unique_ids = list(set(tg_user_ids))
        placeholders = ",".join("?" for _ in unique_ids)

        async with self._db.execute(
            f"""
            SELECT tg_user_id, tz, currency, quiet_hours_start, quiet_hours_end, digest_time,
                   is_admin, created_at
            FROM users WHERE tg_user_id IN ({placeholders})
            """,
            unique_ids,
        ) as cursor:
            rows = await cursor.fetchall()

        users = {}
        for row in rows:
            users[row[0]] = User(
                tg_user_id=row[0],
                tz=row[1],
                currency=row[2],
                quiet_hours_start=row[3],
                quiet_hours_end=row[4],
                digest_time=row[5],
                is_admin=bool(row[6]),
                created_at=row[7],
            )
        return users

    async def set_quiet_hours(self, tg_user_id: int, *, start: str, end: str) -> None:
        await self._db.execute(
            "UPDATE users SET quiet_hours_start = ?, quiet_hours_end = ? WHERE tg_user_id = ?",
            (start, end, tg_user_id),
        )
        await self._db.commit()

    async def set_digest_time(self, tg_user_id: int, value: str | None) -> None:
        """None puts the digest back on quiet_hours_end — see migration 009."""
        await self._db.execute(
            "UPDATE users SET digest_time = ? WHERE tg_user_id = ?", (value, tg_user_id)
        )
        await self._db.commit()

    async def is_admin(self, tg_user_id: int, admin_tg_id: int) -> bool:
        return tg_user_id == admin_tg_id
