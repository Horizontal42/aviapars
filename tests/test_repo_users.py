import pytest
import aiosqlite
from aviapars.db.repo_users import UsersRepo

@pytest.mark.asyncio
async def test_get_many(db: aiosqlite.Connection) -> None:
    repo = UsersRepo(db)
    await repo.get_or_create(1)
    await repo.get_or_create(2)

    users = await repo.get_many([1, 2])
    assert len(users) == 2
    assert 1 in users
    assert 2 in users
    assert users[1].tg_user_id == 1
    assert users[2].tg_user_id == 2

    users_empty = await repo.get_many([])
    assert len(users_empty) == 0

    users_missing = await repo.get_many([3])
    assert len(users_missing) == 0
