from datetime import datetime, timezone
from aviapars.telegram.handlers.status import _parse_moment

def test_parse_moment_valid_iso_string_with_tz() -> None:
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _parse_moment("2026-09-01T12:00:00+00:00") == dt

def test_parse_moment_valid_iso_string_without_tz_assumes_utc() -> None:
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert _parse_moment("2026-09-01T12:00:00") == dt

def test_parse_moment_malformed_iso_string_returns_none() -> None:
    assert _parse_moment("not-a-date") is None

def test_parse_moment_non_string_returns_none() -> None:
    assert _parse_moment(None) is None
    assert _parse_moment(123) is None
