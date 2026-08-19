from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, InlineKeyboardMarkup, Message

MIGRATIONS_DIR = Path(__file__).parent.parent / "data" / "migrations"


@pytest.fixture
async def db(tmp_path: Path) -> aiosqlite.Connection:
    path = tmp_path / "test.db"
    conn = await aiosqlite.connect(path)
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.executescript(migration.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


class FakeScreenBot:
    """Minimal ScreenTransport (telegram/screen.py) — records what a handler
    tried to show instead of talking to real Telegram, same trick as
    test_walking_skeleton.py's FakeBot for Notifier."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._next_message_id = 1000

    async def send_message(
        self, *, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> Message:
        self.sent.append((chat_id, text))
        self._next_message_id += 1
        return Message(
            message_id=self._next_message_id, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type="private")
        )

    async def edit_message_text(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> object:
        assert chat_id is not None and message_id is not None
        self.edited.append((chat_id, message_id, text))
        return None

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        return True

    @property
    def all_shown(self) -> list[str]:
        """Every screen text shown so far, edit or fresh send, in order."""
        return [t for _, t in self.sent] + [t for _, _, t in self.edited]


def make_state(bot: FakeScreenBot, chat_id: int = 555, user_id: int = 555) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage, key)


def make_text_message(chat_id: int, text: str | None, bot: FakeScreenBot, message_id: int = 1) -> Message:
    """A Message with `bot` bound via `.as_()`, same as a real update carries —
    screen_for()/Screen need `.bot` for send/edit/delete. `text=None` stands in
    for a non-text update (sticker/photo/voice/...): aiogram's per-state message
    handlers have no content-type filter, so any of those reach the handler with
    exactly this shape."""
    message = Message(
        message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type="private"), text=text
    )
    message.as_(bot)  # type: ignore[arg-type]
    return message


class DummyRegistry:
    """Stands in for AdapterRegistry in handler tests that never reach the
    search itself (error-path tests) — the real registry needs a live db
    connection and adapters, neither of which those paths ever touch."""

    def all(self) -> list[Any]:
        return []
