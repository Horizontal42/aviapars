from datetime import datetime, timezone
from aviapars.adapters.circuit import CircuitState, record_success

def test_record_success() -> None:
    # Initialize a CircuitState with non-default "bad" values
    bad_state = CircuitState(
        status="down",
        consecutive_failures=3,
        circuit_open_until=datetime.now(timezone.utc),
        escalation=1,
    )

    # Record success
    good_state = record_success(bad_state)

    # Assert all fields are reset to defaults
    assert good_state.status == "ok"
    assert good_state.consecutive_failures == 0
    assert good_state.circuit_open_until is None
    assert good_state.escalation == 0
