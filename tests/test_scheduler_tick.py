from typing import Any
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from aviapars.db.models import Route, User
from aviapars.db.repo_jobs import Job
from aviapars.scheduler.tick import _run_due_notify_jobs, _run_due_routes, run_scheduler_loop

@pytest.fixture
def mock_routes_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.due.return_value = []
    return repo

@pytest.fixture
def mock_users_repo() -> AsyncMock:
    return AsyncMock()

@pytest.fixture
def mock_places_repo() -> AsyncMock:
    return AsyncMock()

@pytest.fixture
def mock_observations_repo() -> AsyncMock:
    return AsyncMock()

@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()

@pytest.fixture
def mock_notifier() -> AsyncMock:
    notifier = AsyncMock()
    notifier.deliver_pending.return_value = []
    return notifier

@pytest.fixture
def mock_jobs_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.due.return_value = []
    repo.reset_stuck_running.return_value = 0
    return repo

@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()

def create_mock_job(id: int = 1) -> Job:
    job = MagicMock(spec=Job)
    job.id = id
    job.kind = "notify"
    job.route_id = 1
    job.priority = 10
    job.payload = {}
    job.state = "pending"
    job.attempts = 1
    job.run_after = None
    return job

def create_mock_route() -> Route:
    route = MagicMock(spec=Route)
    route.id = 1
    route.user_id = 123
    route.title = "T"
    route.origin_place_id = 1
    route.dest_place_id = 2
    route.mode_pref = "plane"
    route.date_mode = "fixed"
    route.fixed_dates = ["2025-01-01"]
    route.window_start_date = None
    route.window_length_days = None
    route.max_price = 100
    route.max_price_currency = "RUB"
    route.poll_interval_min = 60
    route.notification_mode = "immediate"
    route.date_cursor = 0
    return route


async def test_run_due_notify_jobs_no_jobs(test_run_due_notify_jobs_no_jobs: Any) -> None:
    now = datetime.now(timezone.utc)
    mock_jobs_repo.due.return_value = []

    await _run_due_notify_jobs(now, mock_jobs_repo, mock_notifier)

    mock_jobs_repo.due.assert_called_once_with(now=now)
    mock_notifier.deliver_pending.assert_not_called()

async def test_run_due_notify_jobs_handles_success_and_failure(test_run_due_notify_jobs_handles_success_and_failure: Any) -> None:
    now = datetime.now(timezone.utc)

    job1 = create_mock_job(id=1)
    job2 = create_mock_job(id=2)
    job_other = create_mock_job(id=3)
    job_other.kind = "other"

    mock_jobs_repo.due.return_value = [job1, job2, job_other]

    mock_notifier.deliver_pending.return_value = [1]

    await _run_due_notify_jobs(now, mock_jobs_repo, mock_notifier)

    mock_jobs_repo.due.assert_called_once_with(now=now)

    assert mock_jobs_repo.mark_running.await_count == 2
    mock_jobs_repo.mark_running.assert_has_calls([call(1), call(2)])

    mock_notifier.deliver_pending.assert_called_once_with([job1, job2])

    mock_jobs_repo.mark_done.assert_called_once_with(1)
    mock_jobs_repo.mark_failed.assert_called_once_with(2)


async def test_run_due_routes_no_routes(test_run_due_routes_no_routes: Any) -> None:
    now = datetime.now(timezone.utc)
    mock_routes_repo.due.return_value = []

    await _run_due_routes(now, mock_routes_repo, mock_users_repo, mock_places_repo, mock_observations_repo, mock_registry, mock_notifier)

    mock_routes_repo.due.assert_called_once_with(now=now)
    mock_users_repo.get.assert_not_called()

@patch("aviapars.scheduler.tick.process_route", new_callable=AsyncMock)
async def test_run_due_routes_skips_missing_user(test_run_due_routes_skips_missing_user: Any) -> None:
    now = datetime.now(timezone.utc)
    route = create_mock_route()
    mock_routes_repo.due.return_value = [route]
    mock_users_repo.get.return_value = None

    await _run_due_routes(now, mock_routes_repo, mock_users_repo, mock_places_repo, mock_observations_repo, mock_registry, mock_notifier)

    mock_users_repo.get.assert_called_once_with(123)
    mock_process_route.assert_not_called()
    mock_routes_repo.mark_run.assert_called_once_with(1, next_run_at=now + timedelta(minutes=60), ran_at=now)

@patch("aviapars.scheduler.tick.process_route", new_callable=AsyncMock)
async def test_run_due_routes_processes_route(test_run_due_routes_processes_route: Any) -> None:
    now = datetime.now(timezone.utc)
    route = create_mock_route()
    mock_routes_repo.due.return_value = [route]
    user = MagicMock(spec=User)
    user.tg_user_id = 123
    mock_users_repo.get.return_value = user

    await _run_due_routes(now, mock_routes_repo, mock_users_repo, mock_places_repo, mock_observations_repo, mock_registry, mock_notifier)

    mock_users_repo.get.assert_called_once_with(123)
    mock_process_route.assert_called_once_with(route, user=user, registry=mock_registry, places_repo=mock_places_repo, routes_repo=mock_routes_repo, observations_repo=mock_observations_repo, notifier=mock_notifier)
    mock_routes_repo.mark_run.assert_called_once_with(1, next_run_at=now + timedelta(minutes=60), ran_at=now)

@patch("aviapars.scheduler.tick.process_route", new_callable=AsyncMock)
async def test_run_due_routes_handles_exception_and_marks_run(test_run_due_routes_handles_exception_and_marks_run: Any) -> None:
    now = datetime.now(timezone.utc)
    route = create_mock_route()
    mock_routes_repo.due.return_value = [route]
    user = MagicMock(spec=User)
    user.tg_user_id = 123
    mock_users_repo.get.return_value = user
    mock_process_route.side_effect = Exception("test error")

    await _run_due_routes(now, mock_routes_repo, mock_users_repo, mock_places_repo, mock_observations_repo, mock_registry, mock_notifier)

    mock_process_route.assert_called_once()
    mock_routes_repo.mark_run.assert_called_once_with(1, next_run_at=now + timedelta(minutes=60), ran_at=now)


@patch("aviapars.scheduler.tick.RoutesRepo")
@patch("aviapars.scheduler.tick.UsersRepo")
@patch("aviapars.scheduler.tick.PlacesRepo")
@patch("aviapars.scheduler.tick.ObservationsRepo")
@patch("aviapars.scheduler.tick.JobsRepo")
@patch("aviapars.scheduler.tick.ResponseCache")
@patch("aviapars.scheduler.tick._run_due_routes")
@patch("aviapars.scheduler.tick._run_due_notify_jobs")
@patch("asyncio.sleep")
async def test_run_scheduler_loop_flow(test_run_scheduler_loop_flow: Any) -> None:
    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.reset_stuck_running.return_value = 1
    mock_jobs_repo_cls.return_value = mock_jobs_repo

    mock_cache = AsyncMock()
    mock_cache.prune_expired.return_value = 5
    mock_cache_cls.return_value = mock_cache

    mock_observations_repo = AsyncMock()
    mock_observations_repo.prune_older_than.return_value = 2
    mock_observations_repo_cls.return_value = mock_observations_repo

    mock_sleep.side_effect = asyncio.CancelledError()

    with patch("asyncio.get_event_loop") as mock_get_loop:
        mock_loop = MagicMock()
        mock_loop.time.return_value = 4000.0
        mock_get_loop.return_value = mock_loop

        with pytest.raises(asyncio.CancelledError):
            await run_scheduler_loop(mock_db, mock_registry, mock_notifier)

        mock_jobs_repo.reset_stuck_running.assert_called_once()
        mock_run_due_routes.assert_called_once()
        mock_run_due_notify_jobs.assert_called_once()

        mock_cache.prune_expired.assert_called_once()
        mock_observations_repo.prune_older_than.assert_called_once()

        mock_sleep.assert_called_once_with(60.0)

@patch("aviapars.scheduler.tick.RoutesRepo")
@patch("aviapars.scheduler.tick.UsersRepo")
@patch("aviapars.scheduler.tick.PlacesRepo")
@patch("aviapars.scheduler.tick.ObservationsRepo")
@patch("aviapars.scheduler.tick.JobsRepo")
@patch("aviapars.scheduler.tick.ResponseCache")
@patch("aviapars.scheduler.tick._run_due_routes")
@patch("aviapars.scheduler.tick._run_due_notify_jobs")
@patch("asyncio.sleep")
async def test_run_scheduler_loop_catches_exceptions(test_run_scheduler_loop_catches_exceptions: Any) -> None:
    mock_jobs_repo = AsyncMock()
    mock_jobs_repo.reset_stuck_running.return_value = 0
    mock_jobs_repo_cls.return_value = mock_jobs_repo

    mock_cache = AsyncMock()
    mock_cache.prune_expired.side_effect = Exception("prune error")
    mock_cache_cls.return_value = mock_cache

    mock_observations_repo = AsyncMock()
    mock_observations_repo_cls.return_value = mock_observations_repo

    mock_run_due_routes.side_effect = Exception("routes error")
    mock_run_due_notify_jobs.side_effect = Exception("notify error")

    mock_sleep.side_effect = asyncio.CancelledError()

    with patch("asyncio.get_event_loop") as mock_get_loop:
        mock_loop = MagicMock()
        mock_loop.time.return_value = 4000.0
        mock_get_loop.return_value = mock_loop

        with pytest.raises(asyncio.CancelledError):
            await run_scheduler_loop(mock_db, mock_registry, mock_notifier)

        mock_run_due_routes.assert_called_once()
        mock_run_due_notify_jobs.assert_called_once()
        mock_cache.prune_expired.assert_called_once()

        mock_sleep.assert_called_once_with(60.0)
