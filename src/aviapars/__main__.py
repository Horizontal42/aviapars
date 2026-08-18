from __future__ import annotations

import asyncio
import logging

from aviapars.adapters.aeroflot_direct import AeroflotDirectAdapter
from aviapars.adapters.azimuth_direct import AzimuthDirectAdapter
from aviapars.adapters.pobeda_direct import PobedaDirectAdapter
from aviapars.adapters.registry import AdapterRegistry
from aviapars.adapters.rzd_direct import RzdDirectAdapter
from aviapars.adapters.s7_direct import S7DirectAdapter
from aviapars.adapters.travelpayouts import TravelpayoutsAdapter
from aviapars.adapters.tutu_avia_direct import TutuAviaDirectAdapter
from aviapars.adapters.tutu_rail_direct import TutuRailDirectAdapter
from aviapars.adapters.yandex_rasp import YandexRaspAdapter
from aviapars.config import settings
from aviapars.db.engine import connect
from aviapars.db.migrate import migrate
from aviapars.db.repo_digests import DigestsRepo
from aviapars.db.repo_jobs import JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.db.repo_sessions import SessionsRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.notify.notifier import Notifier
from aviapars.scheduler.tick import run_scheduler_loop
from aviapars.systemd_notify import notify, watchdog_loop
from aviapars.telegram.bot import create_bot, create_dispatcher, setup_commands

_WATCHDOG_PING_INTERVAL_S = 100  # aviapars.service sets WatchdogSec=300 — 3x margin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aviapars")


async def main() -> None:
    log.info("aviapars starting up (db=%s)", settings.db_path)
    await migrate()

    async with connect() as db:
        registry = AdapterRegistry(
            db,
            http_concurrency_global=settings.http_concurrency_global,
            http_concurrency_interactive=settings.http_concurrency_interactive,
            http_concurrency_per_source=settings.http_concurrency_per_source,
        )
        registry.register(
            TravelpayoutsAdapter(
                token=settings.travelpayouts_token,
                marker=settings.travelpayouts_marker,
                cache_ttl_s=settings.cache_ttl_s.travelpayouts,
            )
        )
        registry.register(
            YandexRaspAdapter(
                api_key=settings.yandex_rasp_api_key,
                cache_ttl_s=settings.cache_ttl_s.yandex_rasp_price,
            )
        )
        registry.register(RzdDirectAdapter(cache_ttl_s=settings.cache_ttl_s.rzd_direct))
        registry.register(TutuRailDirectAdapter(cache_ttl_s=settings.cache_ttl_s.rzd_direct))
        registry.register(AeroflotDirectAdapter(cache_ttl_s=settings.cache_ttl_s.airline_direct))
        registry.register(AzimuthDirectAdapter(cache_ttl_s=settings.cache_ttl_s.airline_direct))
        registry.register(
            PobedaDirectAdapter(proxy_url=settings.pobeda_proxy_url, cache_ttl_s=settings.cache_ttl_s.airline_direct)
        )
        registry.register(TutuAviaDirectAdapter(cache_ttl_s=settings.cache_ttl_s.airline_direct))

        sessions_repo = SessionsRepo(db)
        registry.register(
            S7DirectAdapter(
                session_getter=lambda: sessions_repo.get("s7_direct"),
                session_setter=lambda payload, expires_at: sessions_repo.save("s7_direct", payload, expires_at),
                cache_ttl_s=settings.cache_ttl_s.airline_direct,
            )
        )

        bot = create_bot(settings.telegram_bot_token, proxy_url=settings.telegram_proxy_url)
        dp = create_dispatcher()

        notifications_repo = NotificationsRepo(db)
        jobs_repo = JobsRepo(db)
        dedupe = DedupeGate(notifications_repo)
        notifier = Notifier(
            bot,
            notifications_repo,
            jobs_repo,
            dedupe,
            DigestsRepo(db),
            cooldown_h=settings.notification_dedupe_cooldown_h,
            daily_cap=settings.notifications_per_route_per_day,
            source_names={a.id: a.display_name for a in registry.all()},
        )

        shutdown_event = asyncio.Event()

        def _on_scheduler_done(task: asyncio.Task[None]) -> None:
            # run_scheduler_loop is a `while True` — it should only ever end via
            # cancellation. If it exits any other way (an unhandled exception past its own
            # try/except, or just returning), every route silently stops being polled while
            # the bot keeps answering commands and /status keeps reporting sources as fine —
            # there's otherwise no signal anywhere that this happened.
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error("scheduler loop died unexpectedly, routes will no longer be polled", exc_info=exc)
            else:
                log.error("scheduler loop exited unexpectedly, routes will no longer be polled")

        scheduler_task = asyncio.create_task(run_scheduler_loop(db, registry, notifier))
        scheduler_task.add_done_callback(_on_scheduler_done)
        watchdog_task = asyncio.create_task(watchdog_loop(_WATCHDOG_PING_INTERVAL_S))
        notify("READY=1")
        try:
            await setup_commands(bot)
            polling_task = asyncio.create_task(
                dp.start_polling(bot, db=db, registry=registry, notifier=notifier, shutdown_event=shutdown_event)
            )
            shutdown_wait = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait({polling_task, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task is polling_task:
                    task.result()  # re-raise if polling itself failed
        finally:
            scheduler_task.cancel()
            watchdog_task.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
