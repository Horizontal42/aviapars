from datetime import datetime, timedelta, timezone

import pytest

from aviapars.adapters.circuit import CircuitState, is_open

NOW = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("circuit_open_until", "expected"),
    [
        (None, False),
        (NOW + timedelta(seconds=1), True),
        (NOW, False),
        (NOW - timedelta(seconds=1), False),
    ],
)
def test_is_open(circuit_open_until: datetime | None, expected: bool) -> None:
    state = CircuitState(circuit_open_until=circuit_open_until)
    assert is_open(state, NOW) is expected
