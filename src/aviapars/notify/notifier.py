from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from aviapars.db.models import Route, User
from aviapars.db.repo_digests import DigestsRepo, PendingDigest
from aviapars.db.repo_jobs import Job, JobsRepo
from aviapars.db.repo_notifications import NotificationsRepo
from aviapars.domain.combiner import Itinerary
from aviapars.notify.dedupe import DedupeGate
from aviapars.telegram.format import format_deal_card

# A digest is one message per day by construction, so the shared per-route daily cap
# (notifications_per_route_per_day, 3) would never bind and would only be misleading if it
# ever did. Passing 1 keeps the DB-backed gate agreeing with what digest mode promises rather
# than silently permitting two more messages the mode says can't exist. The hash cooldown is
# left as configured: "the same itinerary at the same price two mornings running" is exactly
# the repetition that gate is for, and 24h between digests clears a 12h cooldown anyway.
DIGEST_DAILY_CAP = 1


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
        digests_repo: DigestsRepo,
        *,
        cooldown_h: int,
        daily_cap: int,
        source_names: dict[str, str] | None = None,
    ) -> None:
        self._bot = bot
        self._repo = notifications_repo
        self._jobs = jobs_repo
        self._dedupe = dedupe
        self._digests = digests_repo
        self._cooldown_h = cooldown_h
        self._daily_cap = daily_cap
        self._source_names = source_names or {}

    async def send_deal(
        self, *, user: User, route: Route, itinerary: Itinerary, confirmed: bool, now: datetime | None = None
    ) -> bool:
        """Returns True if delivered now, False if suppressed or deferred to
        quiet hours (§9 anti-noise, still applies alongside the threshold gate
        in domain/dealer.py). `itinerary` may be a direct offer (one leg) or
        a hub-composed one (two, §8) — dedupe/records key off a hash of the
        whole itinerary, not a single offer. `now` is injectable for tests;
        real callers never pass it."""
        now = now or datetime.now(timezone.utc)
        all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
        # local, not UTC — see telegram/format.py's format_deal_card for why.
        depart_date = all_legs[0].depart_local.date().isoformat()
        mode = "+".join(dict.fromkeys(leg.mode.value for leg in all_legs))
        itinerary_hash = _combined_hash(itinerary)
        dedupe_key = f"{route.id}:{itinerary_hash}:{now.date().isoformat()}"

        allowed = await self._dedupe.can_send(
            route_id=route.id,
            itinerary_hash=itinerary_hash,
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
            threshold_minor=route.max_price,
            confirmed=confirmed,
            source_names=self._source_names,
        )
        tier = "confirmation" if confirmed else "discovery"

        if is_quiet_hours(user, now):
            # Record now, not at delivery — can_send() above reads notifications_sent, and
            # without this the dedupe gate can't see a deferred deal at all: the same price
            # would be re-enqueued on every poll cycle for the rest of the night instead of
            # being suppressed by the cooldown it's supposed to respect.
            await self._repo.record(
                route_id=route.id,
                dedupe_key=dedupe_key,
                itinerary_hash=itinerary_hash,
                price_minor=itinerary.price_minor,
                mode=mode,
                depart_date=depart_date,
                tier_at_send=tier,
            )
            await self._jobs.enqueue(
                "notify",
                route_id=route.id,
                run_after=next_quiet_hours_end(user, now),
                payload={
                    "chat_id": user.tg_user_id,
                    "text": text,
                },
            )
            return False

        await self._bot.send_message(user.tg_user_id, text)
        await self._repo.record(
            route_id=route.id,
            dedupe_key=dedupe_key,
            itinerary_hash=itinerary_hash,
            price_minor=itinerary.price_minor,
            mode=mode,
            depart_date=depart_date,
            tier_at_send=tier,
        )
        return True

    async def stage_digest(
        self, *, user: User, route: Route, itinerary: Itinerary, confirmed: bool, now: datetime | None = None
    ) -> bool:
        """Digest half of the threshold crossing: instead of messaging now, record
        this itinerary as the best qualifying one of the user's current digest day.
        Returns True if it actually became the day's best. `scheduler/tick.py`
        delivers it when the window closes.

        The card is rendered here, not at send time, for the same reason the
        quiet-hours job stores rendered text: the Offer objects behind it are
        gone by morning, and re-rendering from stored scalars would mean either
        a second schema for legs or a worse card.
        """
        now = now or datetime.now(timezone.utc)
        all_legs = [leg for offer in itinerary.offers for leg in offer.legs]
        local_now = now.astimezone(ZoneInfo(user.tz))
        card = format_deal_card(
            route.title,
            itinerary,
            threshold_minor=route.max_price,
            confirmed=confirmed,
            source_names=self._source_names,
            digest_found_at_local=local_now,
        )
        return await self._digests.stage(
            route_id=route.id,
            day=digest_day(user, now).isoformat(),
            price_minor=itinerary.price_minor,
            currency=itinerary.currency,
            itinerary_hash=_combined_hash(itinerary),
            mode="+".join(dict.fromkeys(leg.mode.value for leg in all_legs)),
            # local, not UTC — see telegram/format.py's format_deal_card for why.
            depart_date=all_legs[0].depart_local.date().isoformat(),
            tier_at_send="confirmation" if confirmed else "discovery",
            card_text=card,
        )

    async def send_digest(
        self, *, user: User, route: Route, pending: PendingDigest, now: datetime | None = None
    ) -> bool:
        """Delivers one already-claimed digest. Quiet hours need no deferral here:
        the window boundary *is* quiet_hours_end, so a digest fires exactly when
        the user's quiet hours end and can never land inside them (the caller
        still skips a window that only came due because the bot was down)."""
        now = now or datetime.now(timezone.utc)
        allowed = await self._dedupe.can_send(
            route_id=route.id,
            itinerary_hash=pending.itinerary_hash,
            now=now,
            cooldown_h=self._cooldown_h,
            daily_cap=DIGEST_DAILY_CAP,
            day_start_utc=_local_day_start_utc(user, now),
        )
        if not allowed:
            return False
        await self._bot.send_message(user.tg_user_id, pending.card_text)
        await self._repo.record(
            route_id=route.id,
            dedupe_key=f"{route.id}:digest:{pending.day}",
            itinerary_hash=pending.itinerary_hash,
            price_minor=pending.price_minor,
            mode=pending.mode,
            depart_date=pending.depart_date,
            tier_at_send=pending.tier_at_send,
        )
        return True

    async def deliver_pending(self, job: Job) -> None:
        """Called by the scheduler for due kind='notify' jobs — the delivery
        half of a quiet-hours deferral from send_deal above. The notifications_sent
        row was already written when the job was enqueued (see send_deal) — writing
        it again here would violate dedupe_key's UNIQUE constraint."""
        p = job.payload
        await self._bot.send_message(p["chat_id"], p["text"])


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


def digest_day(user: User, now_utc: datetime) -> date:
    """Which digest window `now` belongs to, named by its local start date.

    Windows run quiet_hours_end -> quiet_hours_end rather than midnight ->
    midnight, which reuses a time-of-day the user has already chosen instead of
    inventing a second picker for "when should the daily summary arrive", and
    lands the message at the moment they've said they're willing to be disturbed
    again. A window is due once `digest_day(now)` has moved past the staged
    row's day — a comparison of two dates, so a 60s tick that runs many times
    inside the same due window still describes the same single window (the
    conditional claim in DigestsRepo is what makes acting on it once-only).
    """
    local = now_utc.astimezone(ZoneInfo(user.tz))
    end = _parse_hhmm(user.quiet_hours_end)
    return local.date() if local.time() >= end else local.date() - timedelta(days=1)


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
