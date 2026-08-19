from __future__ import annotations

from datetime import date, timedelta

from aviapars.domain.dates import (
    any_date,
    around_date,
    date_range,
    exact_date,
    parse_date,
    parse_date_range,
    resolve_date_plan,
    to_route_fields,
    window,
)

TODAY = date(2026, 9, 1)


def test_exact_date_is_a_single_day_window() -> None:
    assert window(exact_date(date(2026, 9, 10)), TODAY) == (date(2026, 9, 10),)


def test_around_date_spans_both_directions() -> None:
    days = window(around_date(date(2026, 9, 10)), TODAY)
    assert days[0] == date(2026, 9, 7)
    assert days[-1] == date(2026, 9, 13)
    assert len(days) == 7


def test_range_is_inclusive_on_both_ends() -> None:
    days = window(date_range(date(2026, 9, 10), date(2026, 9, 12)), TODAY)
    assert days == (date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12))


def test_any_date_window_rolls_with_today() -> None:
    sel = any_date(length_days=5)
    assert window(sel, TODAY)[0] == TODAY
    assert window(sel, TODAY + timedelta(days=3))[0] == TODAY + timedelta(days=3)


def test_absolute_window_does_not_roll() -> None:
    """The whole reason window_start_date exists: an explicit user range must
    stay put while a "любая дата" window follows today forward."""
    sel = date_range(date(2026, 9, 10), date(2026, 9, 20))
    assert window(sel, TODAY)[0] == date(2026, 9, 10)
    assert window(sel, TODAY + timedelta(days=5))[0] == date(2026, 9, 10)


def test_past_days_are_dropped_from_the_window() -> None:
    sel = date_range(date(2026, 9, 10), date(2026, 9, 20))
    days = window(sel, date(2026, 9, 15))
    assert days[0] == date(2026, 9, 15)
    assert days[-1] == date(2026, 9, 20)


def test_fully_past_window_is_empty() -> None:
    sel = date_range(date(2026, 8, 1), date(2026, 8, 10))
    assert window(sel, TODAY) == ()
    plan, _ = resolve_date_plan(sel, today=TODAY, probe_budget=7)
    assert plan.is_empty


def test_narrow_window_needs_no_rotation() -> None:
    sel = date_range(date(2026, 9, 10), date(2026, 9, 13))
    plan, cursor = resolve_date_plan(sel, today=TODAY, probe_budget=7)
    assert plan.probe_dates == plan.batch_dates
    assert cursor == 0


def test_batch_sources_always_see_the_whole_window() -> None:
    sel = any_date(length_days=30)
    plan, _ = resolve_date_plan(sel, today=TODAY, probe_budget=7)
    assert len(plan.batch_dates) == 30
    assert len(plan.probe_dates) == 7


def test_cursor_rotates_through_the_window_and_wraps() -> None:
    sel = any_date(length_days=10)
    seen: list[date] = []
    cursor = 0
    for _ in range(4):
        plan, cursor = resolve_date_plan(sel, today=TODAY, probe_budget=3, cursor=cursor)
        seen.extend(plan.probe_dates)
    assert set(seen) == set(window(sel, TODAY))  # every date covered within a few cycles
    assert seen[:3] == list(window(sel, TODAY)[:3])
    assert seen[9] == window(sel, TODAY)[9]
    assert seen[10] == window(sel, TODAY)[0]  # wrapped


def test_narrowed_caps_probes_but_keeps_batch_dates() -> None:
    sel = any_date(length_days=30)
    plan, _ = resolve_date_plan(sel, today=TODAY, probe_budget=7)
    narrow = plan.narrowed(1)
    assert len(narrow.probe_dates) == 1
    assert narrow.batch_dates == plan.batch_dates


def test_route_fields_keep_absolute_and_rolling_anchors_apart() -> None:
    absolute = to_route_fields(date_range(date(2026, 9, 10), date(2026, 9, 20)))
    assert absolute.date_mode == "flexible"
    assert absolute.window_start_date == "2026-09-10"
    assert absolute.window_start_offset_days is None
    assert absolute.window_length_days == 11

    rolling = to_route_fields(any_date(length_days=30))
    assert rolling.window_start_date is None
    assert rolling.window_start_offset_days == 0

    fixed = to_route_fields(exact_date(date(2026, 9, 10)))
    assert fixed.date_mode == "fixed"
    assert fixed.fixed_dates == ["2026-09-10"]


def test_parse_date_accepts_only_the_documented_format() -> None:
    assert parse_date("18.09.2026") == date(2026, 9, 18)
    assert parse_date(" 18.09.2026 ") == date(2026, 9, 18)
    assert parse_date("2026-09-18") is None
    assert parse_date("18/09/2026") is None
    assert parse_date("чушь") is None


def test_parse_date_accepts_a_two_digit_year() -> None:
    assert parse_date("18.09.26") == date(2026, 9, 18)
    assert parse_date("18.09.99") == date(2099, 9, 18)
    assert parse_date("18.09.00") == date(2000, 9, 18)


def test_parse_date_rejects_a_bare_number() -> None:
    # The owner's actual report: typed just the day/year digits with no dots while
    # picking a flexible date. Must fail cleanly, not get misread as a year.
    assert parse_date("26") is None
    assert parse_date("18092026") is None


def test_parse_date_rejects_nonsense_calendar_values() -> None:
    assert parse_date("30.02.2026") is None  # Feb 30 doesn't exist
    assert parse_date("31.04.2026") is None  # April has 30 days
    assert parse_date("00.09.2026") is None
    assert parse_date("18.13.2026") is None


def test_parse_date_rejects_three_digit_year() -> None:
    assert parse_date("18.09.202") is None


def test_parse_date_range_accepts_two_digit_years() -> None:
    assert parse_date_range("10.09.26 20.09.26") == (date(2026, 9, 10), date(2026, 9, 20))


def test_parse_date_range_accepts_common_separators() -> None:
    expected = (date(2026, 9, 10), date(2026, 9, 20))
    assert parse_date_range("10.09.2026 20.09.2026") == expected
    assert parse_date_range("10.09.2026-20.09.2026") == expected
    assert parse_date_range("10.09.2026 — 20.09.2026") == expected
    assert parse_date_range("10.09.2026..20.09.2026") == expected


def test_parse_date_range_rejects_backwards_and_incomplete_input() -> None:
    assert parse_date_range("20.09.2026 10.09.2026") is None
    assert parse_date_range("10.09.2026") is None
    assert parse_date_range("10.09.2026 20.09.2026 30.09.2026") is None
