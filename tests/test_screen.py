from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from aviapars.telegram.screen import SCREEN_KEY, Screen, screen_for

CHAT_ID = 4242


class FakeBot:
    """The three Bot calls Screen makes (ScreenTransport), recorded."""

    def __init__(self, *, edit_error: str | None = None) -> None:
        self.sent: list[str] = []
        self.edited: list[tuple[int | None, str]] = []
        self.deleted: list[int] = []
        self._edit_error = edit_error
        self._next_id = 900

    async def send_message(self, *, chat_id, text, reply_markup=None):  # type: ignore[no-untyped-def]
        self._next_id += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, *, chat_id=None, message_id=None, reply_markup=None):  # type: ignore[no-untyped-def]
        if self._edit_error is not None:
            raise TelegramBadRequest(method=None, message=self._edit_error)  # type: ignore[arg-type]
        self.edited.append((message_id, text))
        return True

    async def delete_message(self, *, chat_id, message_id):  # type: ignore[no-untyped-def]
        self.deleted.append(message_id)
        return True


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=7))


def make_message(bot: FakeBot, message_id: int = 55) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=7, is_bot=False, first_name="U"),
        text="Сочи",
    )
    return message.as_(bot)  # type: ignore[arg-type]


def make_query(bot: FakeBot, *, message: Message | None) -> CallbackQuery:
    query = CallbackQuery(
        id="q1",
        from_user=User(id=7, is_bot=False, first_name="U"),
        chat_instance="ci",
        data="mn:root",
        message=message,
    )
    return query.as_(bot)  # type: ignore[arg-type]


async def test_first_render_sends_and_remembers_the_anchor() -> None:
    bot, state = FakeBot(), make_state()
    screen = Screen(bot, CHAT_ID, state)

    await screen.show("привет")

    assert bot.sent == ["привет"]
    assert bot.edited == []
    assert (await state.get_data())[SCREEN_KEY] == screen.anchor_id


async def test_second_render_edits_the_same_message() -> None:
    bot, state = FakeBot(), make_state()
    screen = Screen(bot, CHAT_ID, state, anchor_id=101)

    await screen.show("шаг 1")
    await screen.show("шаг 2")

    assert bot.sent == []
    assert bot.edited == [(101, "шаг 1"), (101, "шаг 2")]


async def test_typed_input_is_deleted_once() -> None:
    bot, state = FakeBot(), make_state()
    screen = Screen(bot, CHAT_ID, state, anchor_id=101, input_message_id=88)

    await screen.show("шаг 1")
    await screen.show("шаг 2")

    assert bot.deleted == [88]


async def test_unmodified_screen_is_not_resent() -> None:
    """Same validation error twice in a row is a no-op, not a reason to start a
    second message."""
    bot, state = FakeBot(edit_error="message is not modified: ..."), make_state()
    screen = Screen(bot, CHAT_ID, state, anchor_id=101)

    await screen.show("Введите число, например 8000.")

    assert bot.sent == []
    assert screen.anchor_id == 101


async def test_dead_anchor_falls_back_to_a_new_message() -> None:
    bot, state = FakeBot(edit_error="message to edit not found"), make_state()
    screen = Screen(bot, CHAT_ID, state, anchor_id=101)

    await screen.show("шаг 1")

    assert bot.sent == ["шаг 1"]
    assert screen.anchor_id == 901
    assert (await state.get_data())[SCREEN_KEY] == 901


async def test_undeletable_input_does_not_break_the_render() -> None:
    class Stubborn(FakeBot):
        async def delete_message(self, *, chat_id, message_id):  # type: ignore[no-untyped-def]
            raise TelegramBadRequest(method=None, message="message can't be deleted")  # type: ignore[arg-type]

    bot, state = Stubborn(), make_state()
    screen = Screen(bot, CHAT_ID, state, anchor_id=101, input_message_id=88)

    await screen.show("шаг 1")

    assert bot.edited == [(101, "шаг 1")]


async def test_anchor_survives_state_clear_at_a_flow_boundary() -> None:
    """The wizards clear state on entry; an anchor that died with the clear
    would mean every «☰ Меню» → /add starts a brand-new message."""
    bot, state = FakeBot(), make_state()
    await state.update_data({SCREEN_KEY: 101})

    screen = await screen_for(make_message(bot), state)
    await state.clear()
    await screen.show("Откуда?")

    assert bot.sent == []
    assert bot.edited == [(101, "Откуда?")]
    assert (await state.get_data())[SCREEN_KEY] == 101


async def test_typed_step_anchors_on_state_and_drops_the_typed_line() -> None:
    bot, state = FakeBot(), make_state()
    await state.update_data({SCREEN_KEY: 101})

    screen = await screen_for(make_message(bot, message_id=90), state)
    await screen.show("дальше")

    assert bot.deleted == [90]
    assert bot.edited == [(101, "дальше")]


async def test_tapped_message_becomes_the_anchor() -> None:
    """A tap re-anchors the screen on the message that was actually tapped —
    scrolling back to an older keyboard should keep editing *that* one, not a
    message the user is no longer looking at."""
    bot, state = FakeBot(), make_state()
    await state.update_data({SCREEN_KEY: 101})

    screen = await screen_for(make_query(bot, message=make_message(bot, message_id=77)), state)
    await screen.show("экран")

    assert bot.edited == [(77, "экран")]
    assert bot.deleted == []
    assert (await state.get_data())[SCREEN_KEY] == 77


async def test_query_without_a_message_falls_back_to_the_stored_anchor() -> None:
    bot, state = FakeBot(), make_state()
    await state.update_data({SCREEN_KEY: 101})

    screen = await screen_for(make_query(bot, message=None), state)
    await screen.show("экран")

    assert bot.edited == [(101, "экран")]


@pytest.mark.parametrize("stored", [None, "not-an-id"])
async def test_garbage_anchor_is_ignored(stored: object) -> None:
    bot, state = FakeBot(), make_state()
    if stored is not None:
        await state.update_data({SCREEN_KEY: stored})

    screen = await screen_for(make_message(bot), state)
    await screen.show("экран")

    assert bot.sent == ["экран"]
