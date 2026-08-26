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
    assert result == date_picker._ERRORS["any"]


def test_parse_any_window_length_rejects_float() -> None:
    result = date_picker.parse_selection("any", "12.5", date(2026, 1, 1))
    assert result == date_picker._ERRORS["any"]


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


def test_parse_any_window_length_rejects_negative() -> None:
    assert isinstance(date_picker.parse_selection("any", "-5", date(2026, 1, 1)), str)


def test_parse_around_accepts_a_two_digit_year() -> None:
    # The owner's ±3 days mode with a same-format-but-shorter date.
    sel = date_picker.parse_selection("around", "18.09.26", date(2026, 1, 1))
    assert not isinstance(sel, str)
    assert sel.window_start == date(2026, 9, 15)


def test_parse_around_rejects_a_bare_number_with_a_friendly_error() -> None:
    # The owner's actual report: typed a bare number, no dots, while in "around" mode.
    result = date_picker.parse_selection("around", "26", date(2026, 1, 1))
    assert isinstance(result, str)


def test_parse_around_rejects_empty_and_whitespace_only_input() -> None:
    assert isinstance(date_picker.parse_selection("around", "", date(2026, 1, 1)), str)
    assert isinstance(date_picker.parse_selection("around", "   ", date(2026, 1, 1)), str)


def test_parse_exact_accepts_a_two_digit_year() -> None:
    sel = date_picker.parse_selection("exact", "18.09.26", date(2026, 1, 1))
    assert not isinstance(sel, str)
    assert sel.fixed_dates == (date(2026, 9, 18),)


def test_parse_exact_rejects_past_dates() -> None:
    result = date_picker.parse_selection("exact", "18.09.25", date(2026, 1, 1))
    assert result == date_picker._PAST


def test_parse_around_rejects_past_dates() -> None:
    result = date_picker.parse_selection("around", "01.01.26", date(2026, 1, 5))
    assert result == date_picker._PAST


def test_parse_range_rejects_invalid_format() -> None:
    result = date_picker.parse_selection("range", "not_a_range", date(2026, 1, 1))
    assert result == date_picker._ERRORS["range"]


def test_parse_range_rejects_past_end_date() -> None:
    result = date_picker.parse_selection("range", "01.01.25 15.01.25", date(2026, 1, 1))
    assert result == date_picker._PAST


def test_parse_range_accepts_two_digit_years() -> None:
    sel = date_picker.parse_selection("range", "10.09.26 20.09.26", date(2026, 1, 1))
    assert not isinstance(sel, str)
    assert sel.window_start == date(2026, 9, 10)
