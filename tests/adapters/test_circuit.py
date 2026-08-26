from datetime import datetime, timedelta, timezone

from aviapars.adapters.circuit import (
    CircuitState,
    record_failure,
    record_success,
    is_open,
    FAILURE_THRESHOLD,
    BACKOFF_STEPS_S,
)


def test_is_open() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Not open if open_until is None
    state_closed = CircuitState(circuit_open_until=None)
    assert not is_open(state_closed, now)

    # Open if now < open_until
    state_open = CircuitState(circuit_open_until=now + timedelta(seconds=1))
    assert is_open(state_open, now)

    # Closed if now == open_until
    state_expired_exact = CircuitState(circuit_open_until=now)
    assert not is_open(state_expired_exact, now)

    # Closed if now > open_until
    state_expired_past = CircuitState(circuit_open_until=now - timedelta(seconds=1))
    assert not is_open(state_expired_past, now)

def test_record_success() -> None:
    state_degraded = CircuitState(status="degraded", consecutive_failures=3, escalation=1)
    new_state = record_success(state_degraded)

    assert new_state.status == "ok"
    assert new_state.consecutive_failures == 0
    assert new_state.circuit_open_until is None
    assert new_state.escalation == 0

    state_down = CircuitState(
        status="down",
        consecutive_failures=5,
        circuit_open_until=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        escalation=2
    )
    new_state = record_success(state_down)

    assert new_state.status == "ok"
    assert new_state.consecutive_failures == 0
    assert new_state.circuit_open_until is None
    assert new_state.escalation == 0

def test_record_failure_degraded() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = CircuitState(status="ok", consecutive_failures=0, escalation=0)

    # Below threshold (e.g. 1 failure)
    new_state = record_failure(state, now)
    assert new_state.status == "degraded"
    assert new_state.consecutive_failures == 1
    assert new_state.circuit_open_until is None
    assert new_state.escalation == 0

    # Just before threshold (e.g. 4 failures if threshold is 5)
    state = CircuitState(status="degraded", consecutive_failures=FAILURE_THRESHOLD - 2, escalation=0)
    new_state = record_failure(state, now)
    assert new_state.status == "degraded"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD - 1
    assert new_state.circuit_open_until is None
    assert new_state.escalation == 0


def test_record_failure_down_escalation_0() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Right at the threshold - 1
    state = CircuitState(status="degraded", consecutive_failures=FAILURE_THRESHOLD - 1, escalation=0)

    new_state = record_failure(state, now)

    assert new_state.status == "down"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD
    assert new_state.escalation == 1
    assert new_state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[0])


def test_record_failure_down_escalation_1() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = CircuitState(status="degraded", consecutive_failures=FAILURE_THRESHOLD - 1, escalation=1)

    new_state = record_failure(state, now)

    assert new_state.status == "down"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD
    assert new_state.escalation == 2
    assert new_state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[1])


def test_record_failure_down_escalation_2() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = CircuitState(status="degraded", consecutive_failures=FAILURE_THRESHOLD - 1, escalation=2)

    new_state = record_failure(state, now)

    assert new_state.status == "down"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD
    assert new_state.escalation == 3
    assert new_state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[2])


def test_record_failure_down_escalation_max() -> None:
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Beyond max escalation index, should cap at the last element of BACKOFF_STEPS_S
    # len(BACKOFF_STEPS_S) is 3, so max index is 2. Let's test escalation=5
    state = CircuitState(status="degraded", consecutive_failures=FAILURE_THRESHOLD - 1, escalation=5)

    new_state = record_failure(state, now)

    assert new_state.status == "down"
    assert new_state.consecutive_failures == FAILURE_THRESHOLD
    assert new_state.escalation == 6
    assert new_state.circuit_open_until == now + timedelta(seconds=BACKOFF_STEPS_S[-1])
