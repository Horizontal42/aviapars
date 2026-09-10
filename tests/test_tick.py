import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from aviapars.scheduler.tick import run_scheduler_loop

@pytest.mark.asyncio
async def test_scheduler_loop_recovers_from_route_processing_error():
    """Test that an exception in _run_due_routes does not stop the scheduler loop."""
    with patch("aviapars.scheduler.tick.RoutesRepo") as MockRoutesRepo, \
         patch("aviapars.scheduler.tick.UsersRepo"), \
         patch("aviapars.scheduler.tick.PlacesRepo"), \
         patch("aviapars.scheduler.tick.ObservationsRepo"), \
         patch("aviapars.scheduler.tick.JobsRepo") as MockJobsRepo, \
         patch("aviapars.scheduler.tick.ResponseCache"), \
         patch("aviapars.scheduler.tick._run_due_routes", new_callable=AsyncMock) as mock_run_due_routes, \
         patch("aviapars.scheduler.tick._run_due_notify_jobs", new_callable=AsyncMock) as mock_run_due_notify_jobs, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_jobs_repo = MockJobsRepo.return_value
        mock_jobs_repo.reset_stuck_running = AsyncMock(return_value=0)

        mock_run_due_routes.side_effect = Exception("Mock exception in route processing")
        mock_sleep.side_effect = asyncio.CancelledError()

        db = AsyncMock()
        registry = AsyncMock()
        notifier = AsyncMock()

        try:
            await run_scheduler_loop(db, registry, notifier)
        except asyncio.CancelledError:
            pass

        mock_run_due_routes.assert_called_once()
        mock_run_due_notify_jobs.assert_called_once()

@pytest.mark.asyncio
async def test_scheduler_loop_recovers_from_notify_job_processing_error():
    """Test that an exception in _run_due_notify_jobs does not stop the scheduler loop."""
    with patch("aviapars.scheduler.tick.RoutesRepo"), \
         patch("aviapars.scheduler.tick.UsersRepo"), \
         patch("aviapars.scheduler.tick.PlacesRepo"), \
         patch("aviapars.scheduler.tick.ObservationsRepo"), \
         patch("aviapars.scheduler.tick.JobsRepo") as MockJobsRepo, \
         patch("aviapars.scheduler.tick.ResponseCache"), \
         patch("aviapars.scheduler.tick._run_due_routes", new_callable=AsyncMock) as mock_run_due_routes, \
         patch("aviapars.scheduler.tick._run_due_notify_jobs", new_callable=AsyncMock) as mock_run_due_notify_jobs, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_jobs_repo = MockJobsRepo.return_value
        mock_jobs_repo.reset_stuck_running = AsyncMock(return_value=0)

        mock_run_due_notify_jobs.side_effect = Exception("Mock exception in notify job processing")
        mock_sleep.side_effect = asyncio.CancelledError()

        db = AsyncMock()
        registry = AsyncMock()
        notifier = AsyncMock()

        try:
            await run_scheduler_loop(db, registry, notifier)
        except asyncio.CancelledError:
            pass

        mock_run_due_routes.assert_called_once()
        mock_run_due_notify_jobs.assert_called_once()
