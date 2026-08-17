from __future__ import annotations

from datetime import date, timedelta

from aviapars.domain.dates import (
    AROUND_DAYS,
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

_PROMPTS = {
    "exact": "Введите дату: ДД.ММ.ГГГГ, например 18.09.2026",
    "around": f"Введите опорную дату: ДД.ММ.ГГГГ. Проверю ±{AROUND_DAYS} дня вокруг неё.",
    "range": "Введите начало и конец диапазона: ДД.ММ.ГГГГ ДД.ММ.ГГГГ, например 10.09.2026 20.09.2026",
}

_ERRORS = {
    "exact": "Не понял дату. Формат: ДД.ММ.ГГГГ, например 18.09.2026",
    "around": "Не понял дату. Формат: ДД.ММ.ГГГГ, например 18.09.2026",
    "range": "Не понял диапазон. Две даты через пробел: 10.09.2026 20.09.2026",
}

_PAST = "Эта дата уже в прошлом. Введите дату в будущем."


def prompt_for(kind: str) -> str | None:
    """None means the choice is complete on its own and needs no typing."""
    return _PROMPTS.get(kind)


def selection_for(kind: str) -> DateSelection | None:
    return any_date() if kind == "any" else None


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
