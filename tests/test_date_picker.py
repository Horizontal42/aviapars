from __future__ import annotations

from datetime import date

from aviapars.domain.dates import MAX_WINDOW_DAYS
from aviapars.telegram import date_picker


def test_any_kind_always_needs_a_prompt() -> None:
    # regression: "any" used to resolve instantly to a hardcoded 30-day window
    # (selection_for returning a value), skipping the length prompt entirely.
    assert date_picker.selection_for("any") is None
    assert date_picker.prompt_for("any") is not None


def test_parse_any_window_length_within_bounds() -> None:
    sel = date_picker.parse_selection("any", "45", date(2026, 1, 1))
    assert not isinstance(sel, str)
    assert sel.window_length_days == 45


def test_parse_any_window_length_rejects_non_numeric() -> None:
    result = date_picker.parse_selection("any", "тридцать", date(2026, 1, 1))
    assert isinstance(result, str)


def test_parse_any_window_length_rejects_out_of_range() -> None:
    too_short = date_picker.parse_selection("any", "1", date(2026, 1, 1))
    assert isinstance(too_short, str)
    too_long = date_picker.parse_selection("any", str(MAX_WINDOW_DAYS + 1), date(2026, 1, 1))
    assert isinstance(too_long, str)


def test_parse_any_window_length_accepts_boundaries() -> None:
    lo = date_picker.parse_selection("any", "7", date(2026, 1, 1))
    hi = date_picker.parse_selection("any", str(MAX_WINDOW_DAYS), date(2026, 1, 1))
    assert not isinstance(lo, str) and lo.window_length_days == 7
    assert not isinstance(hi, str) and hi.window_length_days == MAX_WINDOW_DAYS
