from __future__ import annotations

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import Message

from aviapars.db.repo_places import PlacesRepo
from aviapars.telegram.keyboards import place_picker_kb


async def ask_place_pick(
    message: Message, state: FSMContext, db: aiosqlite.Connection, *, next_state: State, storage_key: str
) -> None:
    """Shared by /add and /search (§10: "reuses the exact same resolution
    widget"). Local `places` table lookup, sub-millisecond, no API call."""
    assert message.text is not None
    places = await PlacesRepo(db).search_by_name(message.text)
    if not places:
        await message.answer("Ничего не нашёл. Попробуйте другое название города.")
        return
    await state.update_data({storage_key: places})
    await state.set_state(next_state)
    await message.answer("Выберите вариант:", reply_markup=place_picker_kb(places))
