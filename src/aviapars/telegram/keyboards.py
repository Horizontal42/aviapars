from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aviapars.db.models import Place


class PlacePick(CallbackData, prefix="pl"):
    idx: int


class ModePick(CallbackData, prefix="md"):
    value: str


class RouteAction(CallbackData, prefix="r"):
    action: str  # pause|resume|check|delete
    route_id: int


class SearchTrack(CallbackData, prefix="st"):
    """§10 "💾 Отслеживать" bridge from /search into /add. `idx` refers to the
    offer list stashed in FSM state for the current search — no
    search_sessions table yet (Phase E), so this only works within the same
    conversation, not across a bot restart."""

    idx: int


_KIND_LABEL = {"city": "город", "airport": "аэропорт", "rail_station": "ж/д станция"}


def place_picker_kb(places: list[Place]) -> InlineKeyboardMarkup:
    """§10: `PlacePick` carries a short index, not the place — Telegram caps
    callback_data at 64 bytes, and a place name/id round-trips through the
    FSM-stored candidate list instead."""
    builder = InlineKeyboardBuilder()
    for i, place in enumerate(places):
        kind = _KIND_LABEL.get(place.kind, place.kind)
        label = f"{place.name_ru}, {kind}" + (f" ({place.iata})" if place.iata else "")
        builder.button(text=label, callback_data=PlacePick(idx=i).pack())
    builder.adjust(1)
    return builder.as_markup()


def mode_picker_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✈️ Самолёт", callback_data=ModePick(value="plane").pack())
    builder.button(text="🚂 Поезд", callback_data=ModePick(value="train").pack())
    builder.button(text="🔀 Авто (оба)", callback_data=ModePick(value="auto").pack())
    builder.adjust(1)
    return builder.as_markup()


def search_results_kb(n: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(n):
        builder.button(text=f"💾 Отслеживать №{i + 1}", callback_data=SearchTrack(idx=i).pack())
    builder.adjust(1)
    return builder.as_markup()


def route_card_kb(route_id: int, *, active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if active:
        builder.button(text="⏸ Пауза", callback_data=RouteAction(action="pause", route_id=route_id).pack())
    else:
        builder.button(text="▶️ Возобновить", callback_data=RouteAction(action="resume", route_id=route_id).pack())
    builder.button(text="🔍 Проверить сейчас", callback_data=RouteAction(action="check", route_id=route_id).pack())
    builder.button(text="🗑 Удалить", callback_data=RouteAction(action="delete", route_id=route_id).pack())
    builder.adjust(2, 1)
    return builder.as_markup()
