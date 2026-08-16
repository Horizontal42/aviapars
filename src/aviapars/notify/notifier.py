from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from aviapars.adapters.base import Offer
from aviapars.db.models import Route, User
from aviapars.db.repo_jobs import Job, JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.notify.dedupe import DedupeGate
from aviapars.telegram.format import format_deal_card


class TelegramSender(Protocol):
    """Structural subset of aiogram's Bot that Notifier actually needs — Bot's
    real send_message() has many more optional params and returns a Message,
    both compatible with this narrower Protocol at the call sites that use it."""

    async def send_message(self, chat_id: int, text: str) -> object: ...


class Notifier:
    """Formats, dedupes, respects quiet hours, sends (§2/§9). The only piece
    of the pipeline that touches Telegram for outbound deal messages.
    """

    def __init__(
        self,
        bot: TelegramSender,
        notifications_repo: NotificationsRepo,
        jobs_repo: JobsRepo,
        dedupe: DedupeGate,
        *,
        cooldown_h: int,
        daily_cap: int,
    ) -> None:
        self._bot = bot
        self._repo = notifications_repo
        self._jobs = jobs_repo
        self._dedupe = dedupe
        self._cooldown_h = cooldown_h
        self._daily_cap = daily_cap

    async def send_deal(self, *, user: User, route: Route, offer: Offer, confirmed: bool) -> bool:
        """Returns True if delivered now, False if suppressed or deferred to
        quiet hours (§9 anti-noise, still applies alongside the threshold gate
        in domain/dealer.py)."""
        now = datetime.now(timezone.utc)
        depart_date = min(leg.depart_utc for leg in offer.legs).date().isoformat()
        dedupe_key = f"{route.id}:{offer.itinerary_hash}:{now.date().isoformat()}"

        allowed = await self._dedupe.can_send(
            route_id=route.id,
            itinerary_hash=offer.itinerary_hash,
            now=now,
            cooldown_h=self._cooldown_h,
            daily_cap=self._daily_cap,
        )
        if not allowed:
            return False

        text = format_deal_card(route.title, offer, threshold_minor=route.max_price, confirmed=confirmed)

        if is_quiet_hours(user, now):
            await self._jobs.enqueue(
                "notify",
                route_id=route.id,
                run_after=next_quiet_hours_end(user, now),
                payload={
                    "chat_id": user.tg_user_id,
                    "text": text,
                    "dedupe_key": dedupe_key,
                    "itinerary_hash": offer.itinerary_hash,
                    "price_minor": offer.price_minor,
                    "mode": offer.legs[0].mode.value,
                    "depart_date": depart_date,
                    "tier": offer.tier.value,
                },
            )
            return False

        await self._bot.send_message(user.tg_user_id, text)
        await self._repo.record(
            route_id=route.id,
            dedupe_key=dedupe_key,
            itinerary_hash=offer.itinerary_hash,
            price_minor=offer.price_minor,
            mode=offer.legs[0].mode.value,
            depart_date=depart_date,
            tier_at_send=offer.tier.value,
        )
        return True

    async def deliver_pending(self, job: Job) -> None:
        """Called by the scheduler for due kind='notify' jobs — the delivery
        half of a quiet-hours deferral from send_deal above."""
        p = job.payload
        await self._bot.send_message(p["chat_id"], p["text"])
        await self._repo.record(
            route_id=job.route_id,  # type: ignore[arg-type]
            dedupe_key=p["dedupe_key"],
            itinerary_hash=p["itinerary_hash"],
            price_minor=p["price_minor"],
            mode=p["mode"],
            depart_date=p["depart_date"],
            tier_at_send=p["tier"],
        )


def is_quiet_hours(user: User, now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(user.tz))
    start = _parse_hhmm(user.quiet_hours_start)
    end = _parse_hhmm(user.quiet_hours_end)
    t = local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight, e.g. 23:00-08:00


def next_quiet_hours_end(user: User, now_utc: datetime) -> datetime:
    local = now_utc.astimezone(ZoneInfo(user.tz))
    end = _parse_hhmm(user.quiet_hours_end)
    candidate = datetime.combine(local.date(), end, tzinfo=local.tzinfo)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))
