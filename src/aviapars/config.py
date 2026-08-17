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
    http_concurrency_interactive: int = 1
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
    flex_search_probe_dates: int = 3  # /search is synchronous under a ~12s budget — much tighter

    search_session_ttl_min: int = 30
    search_sessions_per_user_cap: int = 5

    interactive_l1_deadline_s: float = 12
    interactive_l0_deadline_s: float = 1
    interactive_bucket_wait_s: float = 2

    notifications_per_route_per_day: int = 3
    notification_dedupe_cooldown_h: int = 12

    # SOCKS5/HTTP proxy for the bot's Telegram session only — adapters (travelpayouts,
    # yandex_rasp, rzd_direct, s7_direct) stay direct. None = no proxy (default, local dev).
    telegram_proxy_url: str | None = None

    # SOCKS5 proxy for pobeda_direct only — Pobeda's WAF blocks every datacenter/VPN IP
    # tried (VDS itself, 3 VPN exits) but not a genuine residential one, so this points at
    # a relay reachable over the admin's own home Tailscale connection. Unrelated to
    # telegram_proxy_url — different egress, different reason. None = no proxy (direct).
    pobeda_proxy_url: str | None = None

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
