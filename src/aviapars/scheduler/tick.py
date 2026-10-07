from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from aviapars.adapters.cache import ResponseCache
from aviapars.adapters.registry import AdapterRegistry
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_observations import ObservationsRepo
from aviapars.db.repo_places import PlacesRepo
from aviapars.db.repo_routes import RoutesRepo
from aviapars.db.repo_users import UsersRepo
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.worker import process_route

log = logging.getLogger(__name__)

# Both tables only ever grow (source_cache via the registry's cache writes, keyed partly
# on the rotating date-window cursor so the same key rarely recurs; price_observations one
# row per itinerary per offer per cycle) — nothing else in the codebase ever deletes from
# either. Pruned once per hour, not every tick: both are simple indexed DELETEs, cheap at
# this project's scale, but there's no reason to hit them 60x more often than needed.
_PRUNE_EVERY_S = 3600.0


async def run_scheduler_loop(
    db: aiosqlite.Connection,
    registry: AdapterRegistry,
    notifier: Notifier,
    *,
    tick_interval_s: float = 60.0,
) -> None:
    """Tick loop (§6). **Simplified from the full plan for the walking
    skeleton**: due routes are processed inline here rather than going
    through the `jobs` table as `discovery` jobs pulled by a separate worker
    pool — with exactly one registered adapter and a handful of routes,
    that indirection buys nothing yet. Revisit once route/adapter count
    makes sequential processing too slow for the tick interval (§13 notes
    this scheduler/worker split is meant to grow into the job-queue design
    as the roster grows, not before). `notify` jobs (quiet-hours and
    `notification_mode='digest'` deferral alike, §9) DO already go through
    the jobs table since that's the only mechanism that exists for deferred
    delivery.
    """
    routes_repo = RoutesRepo(db)
    users_repo = UsersRepo(db)
    places_repo = PlacesRepo(db)
    observations_repo = ObservationsRepo(db)
    jobs_repo = JobsRepo(db)
    cache = ResponseCache(db)
    last_prune = 0.0

    reset = await jobs_repo.reset_stuck_running()
    if reset:
        log.warning("recovered %d job(s) stuck in 'running' from a previous crash/restart", reset)

    while True:
        now = datetime.now(timezone.utc)
        try:
            await _run_due_routes(now, routes_repo, users_repo, places_repo, observations_repo, registry, notifier)
        except Exception:
            log.exception("scheduler tick: route processing failed")
        try:
            await _run_due_notify_jobs(now, jobs_repo, notifier)
        except Exception:
            log.exception("scheduler tick: notify job processing failed")

        loop_time = asyncio.get_event_loop().time()
        if loop_time - last_prune >= _PRUNE_EVERY_S:
            last_prune = loop_time
            try:
                pruned_cache = await cache.prune_expired()
                pruned_obs = await observations_repo.prune_older_than()
                if pruned_cache or pruned_obs:
                    log.info("pruned %d expired cache rows, %d old observations", pruned_cache, pruned_obs)
            except Exception:
                log.exception("scheduler tick: prune failed")

        await asyncio.sleep(tick_interval_s)


async def _run_due_routes(
    now: datetime,
    routes_repo: RoutesRepo,
    users_repo: UsersRepo,
    places_repo: PlacesRepo,
    observations_repo: ObservationsRepo,
    registry: AdapterRegistry,
    notifier: Notifier,
) -> None:
    for route in await routes_repo.due(now=now):
        next_run = now + timedelta(minutes=route.poll_interval_min)
        try:
            user = await users_repo.get(route.user_id)
            if user is None:
                continue
            await process_route(
                route,
                user=user,
                registry=registry,
                places_repo=places_repo,
                routes_repo=routes_repo,
                observations_repo=observations_repo,
                notifier=notifier,
            )
        except Exception:
            log.exception("route %s poll failed", route.id)
        finally:
            await routes_repo.mark_run(route.id, next_run_at=next_run, ran_at=now)


async def _run_due_notify_jobs(now: datetime, jobs_repo: JobsRepo, notifier: Notifier) -> None:
    """The whole due batch goes to the notifier in one call, not job by job: several deals
    deferred to the same digest slot have to become one message, and that can only be
    decided while looking at all of them together (notifier.deliver_pending)."""
    jobs = [job for job in await jobs_repo.due(now=now) if job.kind == "notify"]
    if not jobs:
        return
    await jobs_repo.mark_running_batch([job.id for job in jobs])

    # We must explicitly convert the return value to a set, as it might be a generator or list
    delivered = set(await notifier.deliver_pending(jobs))

    delivered_ids = [job.id for job in jobs if job.id in delivered]
    failed_ids = [job.id for job in jobs if job.id not in delivered]

    await jobs_repo.mark_done_batch(delivered_ids)
    await jobs_repo.mark_failed_batch(failed_ids)
