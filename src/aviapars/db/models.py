from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class User:
    tg_user_id: int
    tz: str
    currency: str
    quiet_hours_start: str
    quiet_hours_end: str
    is_admin: bool
    created_at: str


@dataclass(slots=True)
class Place:
    id: int
    kind: str  # city|airport|rail_station
    name_ru: str
    name_en: str | None
    iata: str | None
    yandex_code: str | None
    city_id: int | None
    country: str
    region: str | None
    lat: float | None
    lon: float | None
    popularity: int
    is_hub_candidate: bool


@dataclass(slots=True)
class Route:
    id: int
    user_id: int
    title: str
    origin_place_id: int
    dest_place_id: int
    mode_pref: str  # plane|train|auto
    date_mode: str  # fixed|flexible
    fixed_dates: list[str] | None
    window_start_offset_days: int | None
    window_length_days: int | None
    max_price: int
    max_price_currency: str
    pax: int
    allow_hub_routing: bool
    hub_place_ids: list[int] | None
    max_hops: int
    max_transfer_min: int | None
    max_total_hours: float | None
    poll_interval_min: int
    active: bool
    next_run_at: str | None
    last_run_at: str | None
    created_at: str
