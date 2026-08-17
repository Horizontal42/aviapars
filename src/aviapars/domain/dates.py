from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from aviapars.db.models import Route

AROUND_DAYS = 3
ANY_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 180

DATE_INPUT_FORMAT = "%d.%m.%Y"
_RANGE_SPLIT = re.compile(r"\s*(?:\.\.|—|–|-|\s)\s*")


@dataclass(frozen=True, slots=True)
class DateSelection:
    """What dates a route/search covers, in the shape `routes` stores (§4).

    Two anchors, deliberately both kept: `window_start` pins an absolute range
    («с 10.09 по 20.09» must not drift), `window_start_offset_days` a rolling
    one («любая дата в ближайший месяц» must follow today forward). Storing
    only the offset would silently slide a user's explicit range every night.
    """

    mode: str  # fixed|flexible — mirrors routes.date_mode
    fixed_dates: tuple[date, ...] = ()
    window_start: date | None = None
    window_start_offset_days: int | None = None
    window_length_days: int | None = None


@dataclass(frozen=True, slots=True)
class DatePlan:
    """One cycle's worth of dates, already split by what a source costs.

    `batch_dates` is the whole window: a source with `supports_batch_dates`
    (Travelpayouts' month-matrix) returns a month per request, so scanning 30
    dates costs the same one or two requests as scanning one. `probe_dates` is
    a bounded rotating slice for sources that spend one HTTP request per date
    (yandex_rasp, rzd_direct, s7_direct) — see §6's "round-robin 7 dates/cycle".
    """

    batch_dates: tuple[date, ...]
    probe_dates: tuple[date, ...]

    @property
    def is_empty(self) -> bool:
        return not self.batch_dates

    def narrowed(self, cap: int) -> DatePlan:
        return DatePlan(batch_dates=self.batch_dates, probe_dates=self.probe_dates[:cap])


@dataclass(frozen=True, slots=True)
class RouteDateFields:
    date_mode: str
    fixed_dates: list[str] | None
    window_start_date: str | None
    window_start_offset_days: int | None
    window_length_days: int | None


def exact_date(day: date) -> DateSelection:
    return DateSelection(mode="fixed", fixed_dates=(day,))


def around_date(day: date, spread_days: int = AROUND_DAYS) -> DateSelection:
    return DateSelection(
        mode="flexible", window_start=day - timedelta(days=spread_days), window_length_days=spread_days * 2 + 1
    )


def date_range(start: date, end: date) -> DateSelection:
    length = min((end - start).days + 1, MAX_WINDOW_DAYS)
    return DateSelection(mode="flexible", window_start=start, window_length_days=length)


def any_date(length_days: int = ANY_WINDOW_DAYS) -> DateSelection:
    return DateSelection(mode="flexible", window_start_offset_days=0, window_length_days=length_days)


def window(sel: DateSelection, today: date) -> tuple[date, ...]:
    """Concrete dates the selection covers right now, past days dropped."""
    if sel.mode == "fixed":
        return tuple(sorted(d for d in sel.fixed_dates if d >= today))

    if sel.window_start is not None:
        start = sel.window_start
    else:
        start = today + timedelta(days=sel.window_start_offset_days or 0)
    length = sel.window_length_days or ANY_WINDOW_DAYS
    if start < today:
        length -= (today - start).days
        start = today
    length = min(length, MAX_WINDOW_DAYS)
    if length <= 0:
        return ()
    return tuple(start + timedelta(days=i) for i in range(length))


def resolve_date_plan(
    sel: DateSelection, *, today: date, probe_budget: int, cursor: int = 0
) -> tuple[DatePlan, int]:
    """Splits a selection into this cycle's plan and the cursor to store next.

    The cursor is what keeps a wide window affordable without ever going
    stale: each cycle probes a different `probe_budget`-sized slice, so a
    30-day window is fully covered by one-request-per-date sources in a few
    poll cycles instead of costing 30 requests every single cycle.
    """
    days = window(sel, today)
    if not days:
        return DatePlan(batch_dates=(), probe_dates=()), cursor

    budget = max(1, probe_budget)
    if len(days) <= budget:
        return DatePlan(batch_dates=days, probe_dates=days), 0

    n = len(days)
    start = cursor % n
    probes = tuple(days[(start + i) % n] for i in range(budget))
    return DatePlan(batch_dates=days, probe_dates=probes), (start + budget) % n


def to_route_fields(sel: DateSelection) -> RouteDateFields:
    return RouteDateFields(
        date_mode=sel.mode,
        fixed_dates=[d.isoformat() for d in sel.fixed_dates] or None,
        window_start_date=sel.window_start.isoformat() if sel.window_start else None,
        window_start_offset_days=sel.window_start_offset_days,
        window_length_days=sel.window_length_days,
    )


def selection_from_route(route: Route) -> DateSelection:
    if route.date_mode == "fixed":
        return DateSelection(mode="fixed", fixed_dates=tuple(date.fromisoformat(d) for d in (route.fixed_dates or ())))
    return DateSelection(
        mode="flexible",
        window_start=date.fromisoformat(route.window_start_date) if route.window_start_date else None,
        window_start_offset_days=route.window_start_offset_days,
        window_length_days=route.window_length_days,
    )


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), DATE_INPUT_FORMAT).date()
    except ValueError:
        return None


def parse_date_range(text: str) -> tuple[date, date] | None:
    parts = [p for p in _RANGE_SPLIT.split(text.strip()) if p]
    if len(parts) != 2:
        return None
    start = parse_date(parts[0])
    end = parse_date(parts[1])
    if start is None or end is None or end < start:
        return None
    return start, end
