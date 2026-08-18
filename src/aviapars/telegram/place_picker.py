from __future__ import annotations

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State

from aviapars.db.models import Place
from aviapars.db.repo_places import PlacesRepo
from aviapars.telegram.keyboards import menu_only_kb, place_picker_kb
from aviapars.telegram.screen import Screen


async def ask_place_pick(
    screen: Screen,
    state: FSMContext,
    db: aiosqlite.Connection,
    *,
    text: str,
    pick_state: State,
    storage_key: str,
) -> None:
    """Shared by /add and /search (§10: "reuses the exact same resolution
    widget"). Local `places` table lookup, sub-millisecond, no API call."""
    places = await PlacesRepo(db).search_by_name(text)
    if not places:
        await screen.show(
            f"Ничего не нашёл по запросу «{text}». Попробуйте другое название города.",
            reply_markup=menu_only_kb(),
        )
        return
    await state.update_data({storage_key: places})
    await state.set_state(pick_state)
    await screen.show("Выберите вариант:", reply_markup=place_picker_kb(places))


def resolve_pick(candidates: object, idx: int) -> Place | None:
    """The candidate list round-trips through FSM state, so a stale keyboard (bot
    restarted, or a screen scrolled back to) can point at nothing — that is a
    normal outcome to render, not an assertion to trip over."""
    if not isinstance(candidates, list) or not 0 <= idx < len(candidates):
        return None
    place = candidates[idx]
    return place if isinstance(place, Place) else None
