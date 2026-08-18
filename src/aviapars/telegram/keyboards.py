from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aviapars.db.models import Place


class MenuAction(CallbackData, prefix="mn"):
    """Navigation between top-level screens. Handled by a state-agnostic
    router (`handlers/menu.py`) so «☰ Меню» works from inside any wizard step,
    not only from screens that happen to be stateless."""

    action: str  # root|add|routes|search|status|help


class DatePick(CallbackData, prefix="dt"):
    kind: str  # exact|around|range|any


class PlacePick(CallbackData, prefix="pl"):
    idx: int


class ModePick(CallbackData, prefix="md"):
    value: str


class NotifyModePick(CallbackData, prefix="nm"):
    value: str  # instant|digest


class ThresholdScopePick(CallbackData, prefix="ts"):
    value: str  # shared|per_mode


class RouteAction(CallbackData, prefix="r"):
    action: str  # pause|resume|check|search|delete|notify|interval
    route_id: int


class SearchTrack(CallbackData, prefix="st"):
    """§10 "💾 Отслеживать" bridge from /search into /add. `idx` refers to the
    offer list stashed in FSM state for the current search — no
    search_sessions table yet (Phase E), so this only works within the same
    conversation, not across a bot restart."""

    idx: int


_KIND_LABEL = {"city": "город", "airport": "аэропорт", "rail_station": "ж/д станция"}

MENU_BUTTON_TEXT = "☰ Меню"


def _menu_button(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=MENU_BUTTON_TEXT, callback_data=MenuAction(action="root").pack())


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить маршрут", callback_data=MenuAction(action="add").pack())
    builder.button(text="📋 Мои маршруты", callback_data=MenuAction(action="routes").pack())
    builder.button(text="🔎 Разовый поиск", callback_data=MenuAction(action="search").pack())
    builder.button(text="📊 Состояние источников", callback_data=MenuAction(action="status").pack())
    builder.button(text="❓ Помощь", callback_data=MenuAction(action="help").pack())
    builder.adjust(1)
    return builder.as_markup()


def menu_only_kb() -> InlineKeyboardMarkup:
    """Attached to every free-text wizard prompt: a step that asks the user to
    type something still has to offer a way out (§10 button navigation)."""
    builder = InlineKeyboardBuilder()
    _menu_button(builder)
    return builder.as_markup()


def date_mode_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Точная дата", callback_data=DatePick(kind="exact").pack())
    builder.button(text="📅± Дата ±3 дня", callback_data=DatePick(kind="around").pack())
    builder.button(text="🗓 Диапазон дат", callback_data=DatePick(kind="range").pack())
    builder.button(text="🎲 Любая дата (окно N дней)", callback_data=DatePick(kind="any").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


NOTIFY_MODE_LABEL = {"instant": "⚡ Сразу", "digest": "📬 Раз в день"}


def notify_mode_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Сразу, как найдётся", callback_data=NotifyModePick(value="instant").pack())
    builder.button(text="📬 Раз в день, лучшая цена", callback_data=NotifyModePick(value="digest").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def route_saved_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Мои маршруты", callback_data=MenuAction(action="routes").pack())
    builder.button(text="➕ Ещё маршрут", callback_data=MenuAction(action="add").pack())
    _menu_button(builder)
    builder.adjust(2, 1)
    return builder.as_markup()


def routes_list_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить маршрут", callback_data=MenuAction(action="add").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def search_again_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Новый поиск", callback_data=MenuAction(action="search").pack())
    _menu_button(builder)
    builder.adjust(2)
    return builder.as_markup()


def place_picker_kb(places: list[Place]) -> InlineKeyboardMarkup:
    """§10: `PlacePick` carries a short index, not the place — Telegram caps
    callback_data at 64 bytes, and a place name/id round-trips through the
    FSM-stored candidate list instead."""
    builder = InlineKeyboardBuilder()
    for i, place in enumerate(places):
        kind = _KIND_LABEL.get(place.kind, place.kind)
        label = f"{place.name_ru}, {kind}" + (f" ({place.iata})" if place.iata else "")
        builder.button(text=label, callback_data=PlacePick(idx=i).pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


MODE_LABEL = {"plane": "✈️ Самолёт", "train": "🚂 Поезд", "auto": "🔀 Авто"}


def mode_picker_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✈️ Самолёт", callback_data=ModePick(value="plane").pack())
    builder.button(text="🚂 Поезд", callback_data=ModePick(value="train").pack())
    builder.button(text="🔀 Авто (оба)", callback_data=ModePick(value="auto").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def threshold_scope_kb() -> InlineKeyboardMarkup:
    """Only shown for a route watching more than one kind of transport (§9) —
    a single-mode route has nothing to split and never sees this step."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Один порог на всё", callback_data=ThresholdScopePick(value="shared").pack())
    builder.button(text="✈️/🚂 Отдельно по видам", callback_data=ThresholdScopePick(value="per_mode").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def search_results_kb(n: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i in range(n):
        builder.button(text=f"💾 Отслеживать №{i + 1}", callback_data=SearchTrack(idx=i).pack())
    builder.button(text="🔎 Новый поиск", callback_data=MenuAction(action="search").pack())
    _menu_button(builder)
    builder.adjust(1)
    return builder.as_markup()


def route_card_kb(route_id: int, *, active: bool, notification_mode: str = "instant") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if active:
        builder.button(text="⏸ Пауза", callback_data=RouteAction(action="pause", route_id=route_id).pack())
    else:
        builder.button(text="▶️ Возобновить", callback_data=RouteAction(action="resume", route_id=route_id).pack())
    builder.button(text="🔍 Проверить сейчас", callback_data=RouteAction(action="check", route_id=route_id).pack())
    # Labelled with the mode it switches *to*, like the pause/resume button above it —
    # the card text right next to it already states which mode is on now.
    target = "digest" if notification_mode == "instant" else "instant"
    builder.button(
        text=f"🔔 Уведомлять: {NOTIFY_MODE_LABEL[target]}",
        callback_data=RouteAction(action="notify", route_id=route_id).pack(),
    )
    builder.button(text="⏱ Интервал проверки", callback_data=RouteAction(action="interval", route_id=route_id).pack())
    builder.button(text="🔎 Варианты", callback_data=RouteAction(action="search", route_id=route_id).pack())
    builder.button(text="🗑 Удалить", callback_data=RouteAction(action="delete", route_id=route_id).pack())
    _menu_button(builder)
    builder.adjust(2, 1, 2, 1, 1)
    return builder.as_markup()
