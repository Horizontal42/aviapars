from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram.exceptions import TelegramRetryAfter

from aviapars.db.models import Route, User
from aviapars.db.repo_jobs import Job, JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.domain.combiner import Itinerary, itinerary_modes
from aviapars.domain.dealer import threshold_for
from aviapars.notify.dedupe import DedupeGate
from aviapars.telegram.format import format_deal_card

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DealMeta:
    itinerary_hash: str
    dedupe_key: str
    mode: str
    depart_date: str
    tier: str


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
        source_names: dict[str, str] | None = None,
    ) -> None:
        self._bot = bot
        self._repo = notifications_repo
        self._jobs = jobs_repo
        self._dedupe = dedupe
        self._cooldown_h = cooldown_h
        self._daily_cap = daily_cap
        self._source_names = source_names or {}

    def _build_metadata(self, route: Route, itinerary: Itinerary, confirmed: bool, now: datetime) -> _DealMeta:
        all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
        depart_date = all_legs[0].depart_local.date().isoformat()
        mode = "+".join(dict.fromkeys(leg.mode.value for leg in all_legs))
        itinerary_hash = _combined_hash(itinerary)
        dedupe_key = f"{route.id}:{itinerary_hash}:{now.date().isoformat()}"
        tier = "confirmation" if confirmed else "discovery"
        return _DealMeta(
            itinerary_hash=itinerary_hash,
            dedupe_key=dedupe_key,
            mode=mode,
            depart_date=depart_date,
            tier=tier,
        )

    async def _defer_deal(self, user: User, route: Route, itinerary: Itinerary, meta: _DealMeta, text: str, now: datetime) -> bool:
        # Record now, not at delivery — can_send() above reads notifications_sent, and
        # without this the dedupe gate can't see a deferred deal at all: the same price
        # would be re-enqueued on every poll cycle for the rest of the night instead of
        # being suppressed by the cooldown it's supposed to respect. Same reasoning
        # covers digest mode: without recording at enqueue time, a route polled again
        # before its digest fires could queue the same qualifying price twice.
        try:
            await self._repo.record(
                route_id=route.id,
                dedupe_key=meta.dedupe_key,
                itinerary_hash=meta.itinerary_hash,
                price_minor=itinerary.price_minor,
                mode=meta.mode,
                depart_date=meta.depart_date,
                tier_at_send=meta.tier,
            )
        except aiosqlite.IntegrityError:
            # route_id's FK — the route was deleted between crosses_threshold() finding
            # this deal and getting here (a slow multi-adapter search leaves a wide window
            # for that race). Nothing to defer for a route that no longer exists.
            log.warning("route %s vanished before its deferred deal could be recorded", route.id)
            return False
        await self._jobs.enqueue(
            "notify",
            route_id=route.id,
            run_after=next_quiet_hours_end(user, now),
            payload={
                "chat_id": user.tg_user_id,
                "text": text,
            },
        )
        log.info(
            "deferred deal for route %s: %s %s on %s (hash %s)",
            route.id, meta.mode, itinerary.price_minor, meta.depart_date, meta.itinerary_hash,
        )
        return False

    async def _send_immediate(self, user: User, route: Route, itinerary: Itinerary, meta: _DealMeta, text: str) -> bool:
        await _send_with_retry(self._bot, user.tg_user_id, text)
        try:
            await self._repo.record(
                route_id=route.id,
                dedupe_key=meta.dedupe_key,
                itinerary_hash=meta.itinerary_hash,
                price_minor=itinerary.price_minor,
                mode=meta.mode,
                depart_date=meta.depart_date,
                tier_at_send=meta.tier,
            )
        except aiosqlite.IntegrityError:
            # Same race as above, but the message is already sent by this point — recording
            # is bookkeeping for dedupe/history, not a precondition for delivery, so a route
            # deleted out from under us here is a log line, not a reason to report failure.
            log.warning("route %s vanished right after its deal was sent (dedupe row not recorded)", route.id)
        # No log line here before this fix — the only way to answer "why did I get notified
        # three times tonight" was to guess from notifications_sent, which doesn't record
        # *why* a poll considered something new (a different depart_date within a flexible
        # window is a legitimately distinct itinerary_hash, not a duplicate).
        log.info(
            "sent deal for route %s: %s %s on %s (hash %s)",
            route.id, meta.mode, itinerary.price_minor, meta.depart_date, meta.itinerary_hash,
        )
        return True

    async def send_deal(
        self, *, user: User, route: Route, itinerary: Itinerary, confirmed: bool, now: datetime | None = None
    ) -> bool:
        """Returns True if delivered now, False if suppressed or deferred (§9
        anti-noise, still applies alongside the threshold gate in
        domain/dealer.py). `itinerary` may be a direct offer (one leg) or a
        hub-composed one (two, §8) — dedupe/records key off a hash of the
        whole itinerary, not a single offer. `now` is injectable for tests;
        real callers never pass it.

        Delivery timing branches on two independent things, both landing in
        the same deferral mechanism: actual quiet hours, and
        `route.notification_mode == "digest"`. A digest route defers *every*
        qualifying deal — each is still its own message with its own
        dedupe/cooldown/cap accounting, just all delivered together at the
        next quiet_hours_end instead of one by one through the day. This is
        deliberately not "keep only the cheapest deal of the day": two
        distinct qualifying prices found the same day should read as two
        notifications, not collapse into one."""
        now = now or datetime.now(timezone.utc)
        meta = self._build_metadata(route, itinerary, confirmed, now)

        allowed = await self._dedupe.can_send(
            route_id=route.id,
            itinerary_hash=meta.itinerary_hash,
            now=now,
            cooldown_h=self._cooldown_h,
            daily_cap=self._daily_cap,
            day_start_utc=_local_day_start_utc(user, now),
        )
        if not allowed:
            return False

        text = format_deal_card(
            route.title,
            itinerary,
            # the ceiling this itinerary was actually judged against, not necessarily
            # route.max_price — see domain/dealer.threshold_for
            threshold_minor=threshold_for(route, itinerary_modes(itinerary)),
            confirmed=confirmed,
            source_names=self._source_names,
        )

        if is_quiet_hours(user, now) or route.notification_mode == "digest":
            return await self._defer_deal(user, route, itinerary, meta, text, now)

        return await self._send_immediate(user, route, itinerary, meta, text)

    async def deliver_pending(self, job: Job) -> None:
        """Called by the scheduler for due kind='notify' jobs — the delivery
        half of a quiet-hours deferral from send_deal above. The notifications_sent
        row was already written when the job was enqueued (see send_deal) — writing
        it again here would violate dedupe_key's UNIQUE constraint."""
        p = job.payload
        await _send_with_retry(self._bot, p["chat_id"], p["text"])


async def _send_with_retry(bot: TelegramSender, chat_id: int, text: str) -> None:
    """One retry after Telegram's flood wait — a burst of deals into the same chat routinely
    trips per-chat flood control on the very next send. TelegramRetryAfter is a sibling of
    TelegramBadRequest, not a subclass, so an uncaught one here used to fall straight through
    to the scheduler tick's outer `except Exception: log.exception(...)`, silently dropping
    the deal (record() below never runs, so it just waits for the next poll cycle instead of
    delivering now — see screen.py's Screen for the same fix on the menu-render side)."""
    try:
        await bot.send_message(chat_id, text)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await bot.send_message(chat_id, text)


def _combined_hash(itinerary: Itinerary) -> str:
    """Stable identity for dedupe across both a single offer and a
    hub-composed pair — concatenates each leg-offer's own hash so the same
    two-leg combination always produces the same key."""
    joined = "|".join(o.itinerary_hash for o in itinerary.offers)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _local_day_start_utc(user: User, now_utc: datetime) -> datetime:
    """The daily notification cap (§9) is meant as "N per calendar day for
    this user", not "N per UTC day" — without this, a user east of Moscow
    sees their cap reset mid-afternoon local time (or west of it, midway
    through the previous evening), not at their own midnight."""
    local = now_utc.astimezone(ZoneInfo(user.tz))
    start_of_day = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
    return start_of_day.astimezone(timezone.utc)


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
