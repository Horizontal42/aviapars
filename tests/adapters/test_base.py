import pytest

from aviapars.adapters.base import with_dns_retry as with_egress_retries


@pytest.mark.asyncio
async def test_with_egress_retries_success_first_try() -> None:
    calls = 0

    async def mock_call() -> str:
        nonlocal calls
        calls += 1
        return "success"

    result = await with_egress_retries(mock_call)
    assert result == "success"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_egress_retries_success_on_second_try() -> None:
    calls = 0

    async def mock_call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("First call fails")
        return "success"

    result = await with_egress_retries(mock_call)
    assert result == "success"
    assert calls == 2


@pytest.mark.asyncio
async def test_with_egress_retries_fails_after_retries() -> None:
    calls = 0

    async def mock_call() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("Always fails")

    with pytest.raises(ValueError, match="Always fails"):
        await with_egress_retries(mock_call)
    assert calls == 2


@pytest.mark.asyncio
async def test_with_egress_retries_zero_retries() -> None:
    calls = 0

    async def mock_call() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("Always fails")

    with pytest.raises(ValueError, match="Always fails"):
        await with_egress_retries(mock_call, retries=0)
    assert calls == 1
