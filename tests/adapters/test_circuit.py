from datetime import datetime, timedelta, timezone

from aviapars.adapters.circuit import CircuitState, is_open


def test_is_open_when_none() -> None:
    state = CircuitState(circuit_open_until=None)
    now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert is_open(state, now) is False


def test_is_open_when_future() -> None:
    now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    future = now + timedelta(minutes=1)
    state = CircuitState(circuit_open_until=future)
    assert is_open(state, now) is True


def test_is_open_when_exact() -> None:
    now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = CircuitState(circuit_open_until=now)
    assert is_open(state, now) is False


def test_is_open_when_past() -> None:
    now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    state = CircuitState(circuit_open_until=past)
    assert is_open(state, now) is False
