from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from aviapars.adapters.base import Offer, Tier

MIN_DEPARTURE_LEAD = timedelta(hours=3)
CONFIDENCE_FULL_AGE_H = 0.0
CONFIDENCE_FLOOR_AGE_H = 24.0
CONFIDENCE_AT_FLOOR = 0.3


def freshness_gate(offer: Offer, now: datetime) -> Offer | None:
    """See plans/squishy-scribbling-dawn.md §7. Returns None if the offer must
    be rejected outright, otherwise returns the offer with confidence
    downgraded (never upgraded) for its age.
    """
    if offer.expires_at is not None and offer.expires_at < now:
        return None

    departure = min(leg.depart_utc for leg in offer.legs)
    if departure < now + MIN_DEPARTURE_LEAD:
        return None

    if offer.source_cached_at is None and offer.tier == Tier.DISCOVERY:
        return None

    age_confidence = _age_confidence(offer.source_cached_at, now)
    confidence = min(offer.confidence, age_confidence)
    return replace(offer, confidence=confidence)


def _age_confidence(source_cached_at: datetime | None, now: datetime) -> float:
    if source_cached_at is None:
        return 1.0  # confirmation-tier offers may legitimately have no cache timestamp
    age_h = (now - source_cached_at).total_seconds() / 3600
    if age_h <= CONFIDENCE_FULL_AGE_H:
        return 1.0
    if age_h >= CONFIDENCE_FLOOR_AGE_H:
        return CONFIDENCE_AT_FLOOR
    span = CONFIDENCE_FLOOR_AGE_H - CONFIDENCE_FULL_AGE_H
    decayed = 1.0 - (age_h - CONFIDENCE_FULL_AGE_H) / span * (1.0 - CONFIDENCE_AT_FLOOR)
    return decayed
