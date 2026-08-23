from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence

from aviapars.adapters.base import Offer, Tier

# ---------------------------------------------------------------------------
# Cross-source grouping (§8/§9).
#
# The problem this solves: for one route+date, every source that answers produces its
# own Offer, and the combiner used to wrap each of them in its own Itinerary. Aeroflot
# and Travelpayouts reporting the same Пенза→Питер flight therefore looked like two
# unrelated deals, and the owner got two notifications a day for one trip.
#
# The hard part is that "same trip" is usually *not* provable: a discovery-tier source
# (Travelpayouts' month-matrix) gives date + price + duration and no flight number at
# all. So this module does not answer "is it the same flight" with a boolean — it
# answers with the strongest evidence it actually has, and the card says so out loud
# (Match below). Grouping on a guess and asserting certainty would be worse than the
# duplicate messages it replaces.
#
# Deliberately NOT reused for hub composition: `Itinerary.offers` there means
# "sequential legs of one journey", a completely different relation from "several
# sources describing one leg". Groups produced here always share one leg, and a hub
# itinerary never carries alternates.
# ---------------------------------------------------------------------------


class Match(Enum):
    """Strength of the evidence that a group's offers are one physical trip."""

    SAME_FLIGHT = "same_flight"  # carrier+number known on both sides and equal
    LIKELY_SAME = "likely_same"  # no flight number on one side, but durations corroborate
    UNVERIFIED = "unverified"  # nothing comparable beyond route+date — no identity claimed


_MATCH_RANK = {Match.SAME_FLIGHT: 0, Match.LIKELY_SAME: 1, Match.UNVERIFIED: 2}

# How far two durations may sit apart and still count as corroboration. Chosen against
# what the numbers actually mean rather than as a round number: sources measure a trip
# slightly differently (scheduled block time vs. gate-to-gate, minute rounding, a
# schedule variant on the same flight number), which is worth tens of minutes — while
# the thing this must never wave through, a direct flight vs. a one-stop itinerary on
# the same route, differs by hours (Пенза→Питер: ~2ч direct against 5-7ч via Москву).
# The fraction lets a long trip absorb proportionally more noise; the floor keeps a
# short hop from being matched to the minute; the cap stops a very long itinerary from
# opening a window wide enough to swallow a genuinely different routing.
DURATION_TOLERANCE_MIN = 30
DURATION_TOLERANCE_FRACTION = 0.10
DURATION_TOLERANCE_CAP_MIN = 90


@dataclass(frozen=True, slots=True)
class OfferGroup:
    """One trip as far as we can tell, seen by one or more sources.

    `anchor` is the member with the most usable detail (confirmation tier, real flight
    number) — it supplies the legs that get rendered. `alternates` are the other
    sources' reports of the same trip. `price_minor` is the cheapest member's price:
    the group is one thing to buy, and the honest headline for it is the least it can
    be bought for.
    """

    anchor: Offer
    alternates: tuple[Offer, ...]
    match: Match | None  # None for a lone offer — nothing was grouped, nothing to hedge
    key: str

    @property
    def members(self) -> tuple[Offer, ...]:
        return (self.anchor, *self.alternates)

    @property
    def price_minor(self) -> int:
        return min(o.price_minor for o in self.members)

    @property
    def currency(self) -> str:
        return self.anchor.currency

    @property
    def duration_minutes(self) -> int | None:
        return next((o.duration_minutes for o in self.members if o.duration_minutes is not None), None)


def group_offers(offers: Sequence[Offer]) -> list[OfferGroup]:
    """Cluster one leg's offers so that each group is (probably) one trip.

    A candidate joins a cluster only if it is compatible with *every* member, not just
    with the anchor — pairwise-with-anchor would let a chain of individually-plausible
    matches drift into a group whose ends contradict each other.
    """
    clusters: list[list[Offer]] = []
    verdicts: list[Match | None] = []

    for offer in sorted(offers, key=_anchor_rank):
        for index, cluster in enumerate(clusters):
            pairwise = [_match(member, offer) for member in cluster]
            if any(v is None for v in pairwise):
                continue
            cluster.append(offer)
            verdicts[index] = _weakest([verdicts[index], *pairwise])
            break
        else:
            clusters.append([offer])
            verdicts.append(None)

    return [
        OfferGroup(anchor=cluster[0], alternates=tuple(cluster[1:]), match=verdict, key=_group_key(cluster[0]))
        for cluster, verdict in zip(clusters, verdicts)
    ]


def _anchor_rank(offer: Offer) -> tuple[int, int, int, str]:
    """Who anchors a cluster: confirmation tier first, then whoever knows a flight
    number, then the cheaper offer. The anchor's legs are what the notification card
    renders, so the best-informed member has to win — a date_only discovery offer
    anchoring a group would throw away the real departure time sitting right next to it.
    """
    return (
        0 if offer.tier == Tier.CONFIRMATION else 1,
        0 if _flight_key(offer) else 1,
        offer.price_minor,
        offer.source_id,
    )


def _match(a: Offer, b: Offer) -> Match | None:
    """Evidence that `a` and `b` are the same trip, or None for "they are not".

    Symmetric on purpose (including the tolerance base below): the verdict must not
    depend on which member happened to anchor the cluster.
    """
    if a.source_id == b.source_id:
        # Two offers from one source are two trips that source listed — the morning
        # flight and the evening one — never two reports of one trip.
        return None
    if a.currency != b.currency:
        return None
    if _shape(a) != _shape(b):
        return None

    if _carriers_conflict(a, b):
        return None
    key_a, key_b = _flight_key(a), _flight_key(b)
    if key_a is not None and key_b is not None:
        return Match.SAME_FLIGHT if key_a == key_b else None

    dur_a, dur_b = a.duration_minutes, b.duration_minutes
    if dur_a is None or dur_b is None:
        return Match.UNVERIFIED
    if abs(dur_a - dur_b) <= _duration_tolerance(min(dur_a, dur_b)):
        return Match.LIKELY_SAME
    # Both sides know a duration and they disagree — that is positive evidence of two
    # different trips (typically direct vs. via a hub), not merely absence of proof.
    return None


def _shape(offer: Offer) -> tuple[int, int, int, date, str]:
    """Route endpoints, stop count and departure date — the facts every tier reports."""
    first, last = offer.legs[0], offer.legs[-1]
    # local, not UTC: an overnight departure reads back a day early off depart_utc.date()
    # and would be compared against the neighbouring day's offers.
    return (
        first.from_place.place_id,
        last.to_place.place_id,
        len(offer.legs),
        first.depart_local.date(),
        first.mode.value,
    )


def _flight_key(offer: Offer) -> tuple[str, ...] | None:
    """Per-leg flight number, or None if any leg is missing one.

    Digits only: sources disagree about the airline prefix («SU 6321» vs a bare «6321»)
    while never disagreeing about the number itself. Dropping the prefix would be unsafe
    on its own — two airlines can reuse a number — which is why callers check
    `_carriers_conflict` first, where a known airline on both sides has to be compatible.
    """
    key: list[str] = []
    for leg in offer.legs:
        digits = _normalize_number(leg.number or "")
        if not digits:
            return None
        key.append(digits)
    return tuple(key)


def _carriers_conflict(a: Offer, b: Offer) -> bool:
    for leg_a, leg_b in zip(a.legs, b.legs):
        name_a, name_b = _normalize_carrier(leg_a.carrier), _normalize_carrier(leg_b.carrier)
        if not name_a or not name_b:
            continue
        # substring, not equality: the same airline comes back as «Аэрофлот» from one
        # source and «Аэрофлот - российские авиалинии» from another.
        if name_a not in name_b and name_b not in name_a:
            return True
    return False


def _normalize_carrier(name: str | None) -> str:
    return re.sub(r"[^\w]+", "", (name or "").casefold())


def _normalize_number(number: str) -> str:
    return "".join(re.findall(r"\d+", number))


def _duration_tolerance(minutes: int) -> int:
    scaled = round(minutes * DURATION_TOLERANCE_FRACTION)
    return min(max(DURATION_TOLERANCE_MIN, scaled), DURATION_TOLERANCE_CAP_MIN)


def _weakest(verdicts: Sequence[Match | None]) -> Match:
    known = [v for v in verdicts if v is not None]
    return max(known, key=lambda v: _MATCH_RANK[v])


def _group_key(anchor: Offer) -> str:
    """Identity the notifier's cooldown/dedupe keys off: route, mode, departure date.

    Coarser than the clustering above on purpose, and the two answer different
    questions. Clustering asks "may I show these as one flight" and must not claim more
    than the evidence supports. This asks "have I already told them about flying this
    route on this day recently" — which is the user's own notion of one thing, and the
    only formulation that survives how the data actually arrives.

    Live evidence for the coarser key (route 1, 2026-08-23): the same 16.09 trip was
    announced twice, once from a Travelpayouts row and once from Aeroflot — and those
    two never appear in the same search at all, because per-date confirmation adapters
    only probe a rotating slice of a 30-day window (§6) while batch sources see all of
    it. Clustering cannot merge offers it never sees together; keying the cooldown on
    the flight number would leave exactly the duplicate the owner complained about.

    Cost, accepted: a genuinely different flight on the same date is suppressed for the
    rest of the cooldown window. In practice one poll cycle already reports only the
    cheapest qualifying itinerary (scheduler/worker._deals_to_send), so what is lost is
    a second message about a day the user has just been told about.
    """
    first, last = anchor.legs[0], anchor.legs[-1]
    return (
        f"{first.mode.value}|{first.from_place.place_id}|{last.to_place.place_id}|"
        f"{first.depart_local.date().isoformat()}"
    )
