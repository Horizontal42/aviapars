from __future__ import annotations

from datetime import date, timedelta

from aviapars.domain.dates import (
    ANY_WINDOW_DAYS,
    AROUND_DAYS,
    MAX_WINDOW_DAYS,
    DateSelection,
    any_date,
    around_date,
    date_range,
    exact_date,
    parse_date,
    parse_date_range,
)

# Shared by /add and /search, same as place_picker.py — §10's "reuses the exact
# same date-entry steps as the /add wizard", differing only in what the terminal
# step does with the resulting selection.

_ANY_WINDOW_FLOOR = 7

_PROMPTS = {
    "exact": "Напишите дату в формате ДД.ММ.ГГГГ — например, 18.09.2026.",
    "around": f"Напишите опорную дату в формате ДД.ММ.ГГГГ — проверю ±{AROUND_DAYS} дня вокруг неё.",
    "range": "Напишите две даты через пробел — например, 10.09.2026 20.09.2026.",
    "any": (
        f"На сколько дней вперёд смотреть? Число от {_ANY_WINDOW_FLOOR} до {MAX_WINDOW_DAYS} — "
        f"например, {ANY_WINDOW_DAYS}."
    ),
}

_ERRORS = {
    "exact": "⚠️ Не понял дату. Нужен формат ДД.ММ.ГГГГ — например, 18.09.2026.",
    "around": "⚠️ Не понял дату. Нужен формат ДД.ММ.ГГГГ — например, 18.09.2026.",
    "range": "⚠️ Не понял диапазон. Нужны две даты через пробел — например, 10.09.2026 20.09.2026.",
    "any": f"⚠️ Нужно число дней от {_ANY_WINDOW_FLOOR} до {MAX_WINDOW_DAYS} — например, {ANY_WINDOW_DAYS}.",
}

_PAST = "⚠️ Эта дата уже прошла. Напишите дату в будущем."


def prompt_for(kind: str) -> str | None:
    """None means the choice is complete on its own and needs no typing."""
    return _PROMPTS.get(kind)


def selection_for(kind: str) -> DateSelection | None:
    """None means this kind needs a follow-up text prompt (see prompt_for)."""
    return None


def parse_selection(kind: str, text: str, today: date) -> DateSelection | str:
    """Returns the selection, or the Russian error to show the user."""
    if kind == "range":
        parsed = parse_date_range(text)
        if parsed is None:
            return _ERRORS["range"]
        start, end = parsed
        if end < today:
            return _PAST
        return date_range(max(start, today), end)

    if kind == "any":
        try:
            length_days = int(text.strip())
        except ValueError:
            return _ERRORS["any"]
        if not _ANY_WINDOW_FLOOR <= length_days <= MAX_WINDOW_DAYS:
            return _ERRORS["any"]
        return any_date(length_days=length_days)

    day = parse_date(text)
    if day is None:
        return _ERRORS.get(kind, _ERRORS["exact"])
    if kind == "around":
        if day + timedelta(days=AROUND_DAYS) < today:
            return _PAST
        return around_date(day)
    if day < today:
        return _PAST
    return exact_date(day)
