from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import DNSError, ProxyError, Timeout

from aviapars.adapters.base import (
    MAX_EGRESS_ATTEMPTS_PER_CALL,
    EgressPool,
    ResultStatus,
    SearchResult,
    is_egress_failure,
)
from aviapars.adapters.circuit import BACKOFF_STEPS_S

_T0 = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class _Clock:
    """Controls what `EgressPool` thinks the time is — cooldown expiry is the whole
    point of the pool and there is no way to observe it without moving the clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = _T0
        monkeypatch.setattr("aviapars.adapters.base.utcnow", lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _result(status: ResultStatus) -> SearchResult:
    return SearchResult(status=status, offers=[], partial=False)


class _Recorder:
    """An `attempt` callable that answers per egress and remembers who was asked."""

    def __init__(self, answers: dict[str | None, ResultStatus]) -> None:
        self.answers = answers
        self.seen: list[str | None] = []

    async def __call__(self, url: str | None) -> SearchResult:
        self.seen.append(url)
        return _result(self.answers.get(url, ResultStatus.OK))


async def test_empty_pool_means_direct() -> None:
    pool = EgressPool()
    attempt = _Recorder({})
    result = await pool.run(attempt)
    assert attempt.seen == [None]
    assert result.status == ResultStatus.OK


async def test_rotates_to_next_egress_within_one_call() -> None:
    pool = EgressPool(["a", "b"])
    attempt = _Recorder({"a": ResultStatus.BLOCKED})
    result = await pool.run(attempt)
    assert attempt.seen == ["a", "b"]
    assert result.status == ResultStatus.OK


async def test_block_is_sticky_so_the_next_call_starts_further_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of the pool over the old two-step cascade: a blocked egress is
    # remembered, so later calls don't re-probe it. Re-hitting a source that just blocked
    # you is what earned the VDS a ban from tutu.ru in the first place.
    _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))

    second = _Recorder({"a": ResultStatus.BLOCKED})
    await pool.run(second)
    assert second.seen == ["b"]


async def test_one_call_never_walks_more_than_the_attempt_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 4-entry pool must not quadruple one search()'s worst case — convergence happens
    # across calls (via the cooldowns left behind), not inside one.
    _Clock(monkeypatch)
    pool = EgressPool(["a", "b", "c", "d"])
    attempt = _Recorder({u: ResultStatus.BLOCKED for u in ("a", "b", "c", "d")})
    result = await pool.run(attempt)

    assert len(attempt.seen) == MAX_EGRESS_ATTEMPTS_PER_CALL == 2
    assert attempt.seen == ["a", "b"]
    assert result.status == ResultStatus.BLOCKED

    # ...and the next call picks up where that one left off rather than starting over.
    followup = _Recorder({u: ResultStatus.BLOCKED for u in ("a", "b", "c", "d")})
    await pool.run(followup)
    assert followup.seen == ["c", "d"]


async def test_cooldown_expiry_returns_to_the_preferred_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pool that only moves forward would let one bad hour permanently downgrade a source
    # to its worst egress; the first backoff step expiring makes "a" eligible again for free.
    clock = _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))

    clock.advance(BACKOFF_STEPS_S[0] - 1)
    still_cooling = _Recorder({})
    await pool.run(still_cooling)
    assert still_cooling.seen == ["b"]

    clock.advance(2)
    recovered = _Recorder({})
    await pool.run(recovered)
    assert recovered.seen == ["a"]


async def test_repeated_blocks_escalate_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))

    clock.advance(BACKOFF_STEPS_S[0])
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))  # blocked again on re-probe

    # second block buys the second step of the ladder, not the first one over again
    clock.advance(BACKOFF_STEPS_S[0] + 1)
    too_soon = _Recorder({})
    await pool.run(too_soon)
    assert too_soon.seen == ["b"]

    clock.advance(BACKOFF_STEPS_S[1])
    eventually = _Recorder({})
    await pool.run(eventually)
    assert eventually.seen == ["a"]


async def test_success_resets_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))

    clock.advance(BACKOFF_STEPS_S[0])
    await pool.run(_Recorder({}))  # "a" is eligible again and works

    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))
    clock.advance(BACKOFF_STEPS_S[0])
    back = _Recorder({})
    await pool.run(back)
    assert back.seen == ["a"]  # first step again, not the escalated one


async def test_empty_result_counts_as_proof_the_egress_works(monkeypatch: pytest.MonkeyPatch) -> None:
    # EMPTY means the request was answered and the answer was "no offers" — rotating for
    # that would double a source's request volume for a non-blocking reason.
    _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    attempt = _Recorder({"a": ResultStatus.EMPTY})
    await pool.run(attempt)
    assert attempt.seen == ["a"]

    again = _Recorder({"a": ResultStatus.EMPTY})
    await pool.run(again)
    assert again.seen == ["a"]


async def test_upstream_error_neither_rotates_nor_cools_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Says nothing about the egress either way — a second IP won't fix a broken payload.
    _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    attempt = _Recorder({"a": ResultStatus.UPSTREAM_ERROR})
    result = await pool.run(attempt)
    assert attempt.seen == ["a"]
    assert result.status == ResultStatus.UPSTREAM_ERROR

    again = _Recorder({})
    await pool.run(again)
    assert again.seen == ["a"]


async def test_duplicate_urls_are_collapsed() -> None:
    # config lets every adapter's pool point at the same home relay; a pool listing one
    # egress twice would "rotate" onto the address that just blocked it.
    pool = EgressPool(["a", "a", "b"])
    attempt = _Recorder({"a": ResultStatus.BLOCKED})
    await pool.run(attempt)
    assert attempt.seen == ["a", "b"]


async def test_all_cooling_still_spends_a_request_on_the_stalest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reporting BLOCKED without trying anything is the circuit breaker's job, not the
    # pool's — and the egress whose cooldown expires soonest is the best guess available.
    clock = _Clock(monkeypatch)
    pool = EgressPool(["a", "b"], attempts_per_call=1)
    await pool.run(_Recorder({"a": ResultStatus.BLOCKED}))
    clock.advance(60)
    await pool.run(_Recorder({"b": ResultStatus.BLOCKED}))

    clock.advance(1)
    attempt = _Recorder({})
    await pool.run(attempt)
    assert attempt.seen == ["a"]  # blocked a minute earlier, so it comes off cooldown first


def test_is_egress_failure_flags_a_connection_that_never_completed() -> None:
    assert is_egress_failure(Timeout("Failed to perform, curl: (28) Connection timed out"))
    assert is_egress_failure(CurlConnectionError("Failed to perform, curl: (7) Connection refused"))
    assert is_egress_failure(ProxyError("Failed to perform, curl: (97) proxy handshake failed"))


def test_is_egress_failure_ignores_local_resolver_flakiness() -> None:
    # This VDS's curl_cffi resolver fails on hosts the OS resolves fine (see
    # with_dns_retry) — treating that as a block would cool off a healthy egress.
    assert not is_egress_failure(DNSError("Failed to perform, curl: (6) Could not resolve host"))
    assert not is_egress_failure(Timeout("Failed to perform, curl: (28) Resolving timed out after 5000ms"))


def test_is_egress_failure_ignores_answers_that_arrived_and_were_bad() -> None:
    assert not is_egress_failure(ValueError("Expecting value: line 1 column 1"))
    assert not is_egress_failure(KeyError("departureDateTime"))


async def test_pool_state_survives_interleaved_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    # No lock guards the pool: every mutation is synchronous, so it is atomic under
    # asyncio. This walks two overlapping runs through the pool to prove the bookkeeping
    # doesn't get lost when one call's attempt is in flight while another records.
    _Clock(monkeypatch)
    pool = EgressPool(["a", "b"])
    gate = asyncio.Event()

    async def slow(url: str | None) -> SearchResult:
        if url == "a":
            await gate.wait()
            return _result(ResultStatus.BLOCKED)
        return _result(ResultStatus.OK)

    first = asyncio.create_task(pool.run(slow))
    await asyncio.sleep(0)
    second = _Recorder({"a": ResultStatus.BLOCKED})
    second_task = asyncio.create_task(pool.run(second))
    await asyncio.sleep(0)
    gate.set()

    assert (await first).status == ResultStatus.OK
    assert (await second_task).status == ResultStatus.OK
    # Both overlapping calls paid for "a" (nothing had recorded it yet when they started),
    # but both still found their way to "b" and the block was recorded exactly once.
    assert second.seen == ["a", "b"]
    later = _Recorder({})
    await pool.run(later)
    assert later.seen == ["b"]


async def test_run_returns_the_last_attempt_when_everything_blocks() -> None:
    pool = EgressPool(["a", "b"])
    attempt = _Recorder({"a": ResultStatus.BLOCKED, "b": ResultStatus.BLOCKED})
    result: Any = await pool.run(attempt)
    assert result.status == ResultStatus.BLOCKED
    assert attempt.seen == ["a", "b"]
