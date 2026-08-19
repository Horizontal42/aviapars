from __future__ import annotations

import aiosqlite

from aviapars.telegram.handlers.add import (
    on_date_value_text,
    on_destination_text,
    on_interval_text,
    on_origin_text,
    on_threshold_text,
)
from aviapars.telegram.handlers.routes import on_interval_edit_text
from aviapars.telegram.handlers.search import on_search_date_value_text, on_search_origin_text
from aviapars.telegram.states import AddRoute, EditRoute, SearchFlow

from tests.conftest import DummyRegistry, FakeScreenBot, make_state, make_text_message


async def test_add_date_value_non_text_message_reprompts_instead_of_crashing() -> None:
    """Regression: a sticker/photo/voice message mid-wizard hit `assert message.text
    is not None` and crashed the update with no reply — the owner's "bot died,
    had to back out to the menu" symptom. Same handler, malformed-but-non-text input."""
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.date_value)
    await state.update_data(date_kind="around", origin_title="Пенза", dest_title="Сочи", mode_pref="plane")

    message = make_text_message(555, None, bot)  # text=None: a sticker/photo/voice update
    await on_date_value_text(message, state)  # must not raise

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.date_value.state  # stays put, no forced menu bounce


async def test_search_date_value_non_text_message_reprompts_instead_of_crashing(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(SearchFlow.date_value)
    await state.update_data(date_kind="any", origin_title="Пенза", dest_title="Москва", mode_pref="auto")

    message = make_text_message(555, None, bot)
    await on_search_date_value_text(message, state, db, DummyRegistry())  # type: ignore[arg-type]

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == SearchFlow.date_value.state


async def test_add_threshold_non_text_message_reprompts_instead_of_crashing() -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.threshold)
    await state.update_data(origin_title="Пенза", dest_title="Сочи", mode_pref="plane")

    message = make_text_message(555, None, bot)
    await on_threshold_text(message, state)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.threshold.state


async def test_add_interval_non_text_message_reprompts_instead_of_crashing() -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.interval)
    await state.update_data(origin_title="Пенза", dest_title="Сочи", mode_pref="plane")

    message = make_text_message(555, None, bot)
    await on_interval_text(message, state)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.interval.state


async def test_edit_route_interval_non_text_message_reprompts_instead_of_crashing(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(EditRoute.interval)
    await state.update_data(edit_route_id=1)

    message = make_text_message(555, None, bot)
    await on_interval_edit_text(message, state, db)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == EditRoute.interval.state


async def test_add_origin_non_text_message_reprompts_instead_of_crashing(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.origin)

    message = make_text_message(555, None, bot)
    await on_origin_text(message, state, db)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.origin.state


async def test_add_destination_non_text_message_reprompts_instead_of_crashing(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.destination)
    await state.update_data(origin_title="Пенза")

    message = make_text_message(555, None, bot)
    await on_destination_text(message, state, db)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.destination.state


async def test_search_origin_non_text_message_reprompts_instead_of_crashing(db: aiosqlite.Connection) -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(SearchFlow.origin)

    message = make_text_message(555, None, bot)
    await on_search_origin_text(message, state, db)

    assert any("текстом" in t for t in bot.all_shown)
    assert await state.get_state() == SearchFlow.origin.state


async def test_add_date_value_bare_number_reprompts_and_stays_in_state() -> None:
    """The owner's actual report: typed a bare number (no dots) while in the ±3 days
    "around" mode. parse_date already rejects this via ValueError inside strptime's
    replacement — this proves the *handler*, not just the pure parser, degrades to a
    friendly re-prompt rather than raising or bouncing back to the main menu."""
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.date_value)
    await state.update_data(date_kind="around", origin_title="Пенза", dest_title="Сочи", mode_pref="plane")

    message = make_text_message(555, "26", bot)
    await on_date_value_text(message, state)

    assert any("Не понял дату" in t for t in bot.all_shown)
    assert await state.get_state() == AddRoute.date_value.state


async def test_add_date_value_two_digit_year_is_accepted() -> None:
    bot = FakeScreenBot()
    state = make_state(bot)
    await state.set_state(AddRoute.date_value)
    await state.update_data(date_kind="exact", origin_title="Пенза", dest_title="Сочи", mode_pref="plane")

    message = make_text_message(555, "18.09.26", bot)
    await on_date_value_text(message, state)

    # Success moves the wizard on to the threshold step, not stuck re-prompting for a date.
    assert await state.get_state() == AddRoute.threshold.state
