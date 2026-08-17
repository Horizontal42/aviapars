from __future__ import annotations

import asyncio
import logging

from aviapars.adapters.aeroflot_direct import AeroflotDirectAdapter
from aviapars.adapters.azimuth_direct import AzimuthDirectAdapter
from aviapars.adapters.registry import AdapterRegistry
from aviapars.adapters.rzd_direct import RzdDirectAdapter
from aviapars.adapters.s7_direct import S7DirectAdapter
from aviapars.adapters.travelpayouts import TravelpayoutsAdapter
from aviapars.adapters.yandex_rasp import YandexRaspAdapter
from aviapars.config import settings
from aviapars.db.engine import connect
from aviapars.db.migrate import migrate
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
        registry.register(AeroflotDirectAdapter(cache_ttl_s=settings.cache_ttl_s.airline_direct))
        registry.register(AzimuthDirectAdapter(cache_ttl_s=settings.cache_ttl_s.airline_direct))

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
            cooldown_h=settings.notification_dedupe_cooldown_h,
            daily_cap=settings.notifications_per_route_per_day,
        )

        scheduler_task = asyncio.create_task(run_scheduler_loop(db, registry, notifier))
        watchdog_task = asyncio.create_task(watchdog_loop(_WATCHDOG_PING_INTERVAL_S))
        notify("READY=1")
        try:
            await setup_commands(bot)
            await dp.start_polling(bot, db=db, registry=registry, notifier=notifier)
        finally:
            scheduler_task.cancel()
            watchdog_task.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
