from __future__ import annotations

from pathlib import Path
from typing import Type

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class CacheTtl(BaseSettings):
    travelpayouts: int = 10800
    yandex_rasp_schedule: int = 43200
    yandex_rasp_price: int = 7200
    airline_direct: int = 1800
    rzd_direct: int = 2700


class Settings(BaseSettings):
    """Secrets (required, no default) come from .env / real env vars.
    Tunables (all have defaults) come from config.yaml and can be overridden by
    .env / env vars if also set there. See ARCHITECTURE.md "Config vs secrets".
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        yaml_file=REPO_ROOT / "config.yaml",
        # Not the default: without it the yaml is read in the OS locale encoding, so a
        # config.yaml copied from config.example.yaml (Russian comments, as instructed at the
        # top of that file) blows up with UnicodeDecodeError on a cp1251 Windows box.
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # --- secrets: required, no default ---
    telegram_bot_token: str
    admin_tg_id: int
    travelpayouts_token: str
    travelpayouts_marker: str
    yandex_rasp_api_key: str

    # --- tunables: have defaults, sourced from config.yaml ---
    db_path: str = "./aviapars.db"
    seed_countries: list[str] = Field(default_factory=lambda: ["RU"])

    worker_count: int = 2
    http_concurrency_global: int = 2
    # /search's own slot, separate from the global pool. 2, not 1: the combiner asks every
    # adapter for a leg concurrently now, and a 1-wide interactive lane would re-serialize
    # exactly the path where a human is waiting for the answer.
    http_concurrency_interactive: int = 2
    http_concurrency_per_source: int = 1

    poll_interval_floor_min: int = 15
    poll_interval_default_min: int = 60

    cache_ttl_s: CacheTtl = Field(default_factory=CacheTtl)

    max_combo_candidates: int = 24
    max_hops_cap: int = 2

    # Flexible date windows (§6). Sources that batch a whole month per request always get the
    # full window; these caps only bound sources that spend one request per date.
    flex_probe_dates_per_cycle: int = 7
    flex_hub_probe_dates: int = 1
    # /search still only probes a slice of a wide flexible window live, not the full thing —
    # unrelated to interactive_l1_deadline_s below (that's per-adapter wall-clock time; this is
    # request *volume*, N one-request-per-date sources x this many dates, kept small so a
    # flexible-window /search doesn't fan out into dozens of requests). Full-window coverage is
    # what the background poller is for.
    flex_search_probe_dates: int = 3

    search_session_ttl_min: int = 30
    search_sessions_per_user_cap: int = 5

    # Per-adapter wall-clock ceiling for a live /search call — no longer a hard truncation
    # point (registry._resolve_timeout_s floors at the adapter's own honest total_timeout_s
    # regardless of this value now, same as the scheduler lane always did). What this still
    # controls: it's the value that can *raise* the effective ceiling above an adapter's
    # declared worst case if an operator wants /search to wait even longer than that, and it
    # caps how long /search waits for a busy rate-limit bucket (combiner.py's bucket_wait_s).
    # Raised from the original 12s once the egress pool (ARCHITECTURE.md "Пул egress") made a
    # source's honest worst case routinely exceed 12s — the old hard cap was cutting off a
    # search right as a rotated attempt was about to pay off. 240s comfortably covers every
    # adapter's total_timeout_s in the current roster (s7_direct's 270s is the outlier, and
    # s7 is BLOCKED regardless of wait right now — see ARCHITECTURE.md).
    interactive_l1_deadline_s: float = 240
    interactive_l0_deadline_s: float = 1
    interactive_bucket_wait_s: float = 2

    notifications_per_route_per_day: int = 3
    notification_dedupe_cooldown_h: int = 12

    # SOCKS5/HTTP proxy for the bot's Telegram session only — adapters (travelpayouts,
    # yandex_rasp, s7_direct) stay direct. None = no proxy (default, local dev).
    telegram_proxy_url: str | None = None

    # Egress pools (see ARCHITECTURE.md "Прокси"). Each is an ordered list of SOCKS5/HTTP
    # proxy URLs that one adapter rotates through as they get blocked; a `null` entry means
    # the VDS's own IP with no proxy at all. Order is preference: cheapest and least
    # dependent on anything external first, the admin's home residential relay last, since
    # that one only works while a personal machine is switched on. An empty list means
    # direct-only, which is the local-dev default. Every list is free to name the same relay
    # — it just forwards TCP and doesn't care which adapter's traffic it carries.
    #
    # These are per-adapter rather than one shared pool because the blocks are not the same
    # block: what unbans one site is verified useless (or actively harmful) for another, and
    # the notes on each field are what keeps that knowledge from being re-learned by trial.
    #
    # aeroflot_egress_pool — the WAF blocks by address *class*, so a Russian geolocation
    # doesn't help by itself: `ru-bridge` (the xray outbound on the VDS) is Moscow-located
    # but hosting-ASN and gets blocked intermittently. Preferred anyway as the first entry
    # because it depends on nothing outside the VDS; the residential relay backs it up.
    # socks5h, not socks5: www.aeroflot.ru doesn't resolve from some VDS resolvers, so DNS
    # has to go through the proxy too.
    aeroflot_egress_pool: list[str | None] = Field(default_factory=list)

    # rzd_egress_pool — direct first (`null`): pass.rzd.ru bans this VDS's IP outright, but
    # that recovers on its own the moment the ban lifts and costs nothing to keep probing.
    # `ru-bridge` must NOT be listed here: verified live that RZD doesn't merely block that
    # subnet, it blackholes it — the attempt hangs to the full timeout instead of answering,
    # which is the most expensive possible way for a pool entry to fail.
    rzd_egress_pool: list[str | None] = Field(default_factory=list)

    # pobeda_egress_pool — expected to hold exactly one entry, the residential relay.
    # Pobeda's gate is an explicit VPN/datacenter detector ("выключите VPN"), so every
    # hosting-range egress in this project is useless here by construction, not untested:
    # the VDS itself and three VPN exits were all refused, a home ISP address passed.
    pobeda_egress_pool: list[str | None] = Field(default_factory=list)

    # tutu_egress_pool — shared by tutu_rail_direct and tutu_avia_direct: same platform,
    # same infrastructure, banned together. Direct (`null`) first for the same reason as
    # RZD; then the Paris xray outbound on the VDS (verified live to reach tutu.ru while the
    # VDS's own IP was rate-limited, and unlike the home relay it needs nobody's PC to be
    # on); the residential relay last. Tutu's block is a silent TCP drop with no HTTP status
    # at all, which is why the two adapters classify a failed connection as BLOCKED rather
    # than an upstream error — see adapters/base.is_egress_failure.
    tutu_egress_pool: list[str | None] = Field(default_factory=list)

    # --- per-adapter enable switches ---
    # Every adapter setup_registry() (__main__.py) can register is individually toggleable —
    # flip to False to pull one out of rotation without touching or deleting its code. Started
    # as a one-off for s7_enabled (see below) and generalized: a source can be temporarily
    # broken, mid-migration, or just not worth its timeout budget right now, and "comment out
    # the registration" doesn't survive the next deploy/merge the way a named field does.
    travelpayouts_enabled: bool = True
    # yandex_rasp_enabled — off by default: the free API key never returns `tickets_info` on
    # any segment (confirmed live across every route tested this session — 0 of N segments
    # ever carry a price), so this adapter contributes zero offers, ever, as things stand. Real
    # train prices already come from rzd_direct + tutu_rail_direct. Flip back on only if S7-style
    # legitimate access (a paid/partner Yandex Rasp tier that actually includes ticket prices)
    # is obtained — see ARCHITECTURE.md "yandex_rasp".
    yandex_rasp_enabled: bool = False
    rzd_enabled: bool = True
    tutu_rail_enabled: bool = True
    aeroflot_enabled: bool = True
    azimuth_enabled: bool = True
    pobeda_enabled: bool = True
    tutu_avia_enabled: bool = True
    # s7_enabled — off by default: ibe.s7.ru fronts a Group-IB antibot that binds a trust
    # session to per-request client-computed signature headers, not just cookies. Confirmed
    # live: a real human browser session's own fresh cookies still get BLOCKED when replayed
    # through a different HTTP client, so the adapter's browser-solve fallback always burns
    # its ~90s budget for a guaranteed failure. Full writeup: docs/sources/s7.md and
    # ARCHITECTURE.md "S7". Flip to true only once a legitimate fix lands (e.g. Travelpayouts
    # covering S7 routes makes this moot, or S7 changes their protection) — do not defeat the
    # signing to force it back on, that's circumventing an access control, not scraping a
    # public page.
    s7_enabled: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: real env > .env > config.yaml > field defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


settings = Settings()  # type: ignore[call-arg]  # secrets come from .env at runtime
