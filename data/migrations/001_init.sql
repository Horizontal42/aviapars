-- See plans/squishy-scribbling-dawn.md §4 for the design rationale behind each table/comment.

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE users (
    tg_user_id          INTEGER PRIMARY KEY,
    tz                  TEXT NOT NULL DEFAULT 'Europe/Moscow',
    currency            TEXT NOT NULL DEFAULT 'RUB',
    quiet_hours_start   TEXT NOT NULL DEFAULT '23:00',
    quiet_hours_end     TEXT NOT NULL DEFAULT '08:00',
    is_admin            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- kind: city|airport|rail_station (bus_station arrives with the first bus adapter)
CREATE TABLE places (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('city', 'airport', 'rail_station')),
    name_ru             TEXT NOT NULL,
    name_en             TEXT,
    iata                TEXT,
    yandex_code         TEXT,
    city_id             INTEGER REFERENCES places(id),
    country             TEXT NOT NULL,
    region              TEXT,
    lat                 REAL,
    lon                 REAL,
    popularity          INTEGER NOT NULL DEFAULT 0,
    is_hub_candidate    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_places_name_ru ON places(name_ru);
CREATE INDEX idx_places_country ON places(country);
CREATE INDEX idx_places_hub_candidate ON places(is_hub_candidate) WHERE is_hub_candidate = 1;

-- mode_pref: plane|train|auto (single-valued today, see §4 note on domain.places.allowed_modes)
CREATE TABLE routes (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(tg_user_id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    origin_place_id     INTEGER NOT NULL REFERENCES places(id),
    dest_place_id       INTEGER NOT NULL REFERENCES places(id),
    mode_pref           TEXT NOT NULL CHECK (mode_pref IN ('plane', 'train', 'auto')),
    date_mode           TEXT NOT NULL CHECK (date_mode IN ('fixed', 'flexible')),
    fixed_dates         TEXT,   -- JSON array of ISO dates
    window_start_offset_days INTEGER,
    window_length_days  INTEGER,
    max_price           INTEGER NOT NULL,   -- minor units; required (§9)
    max_price_currency  TEXT NOT NULL,
    pax                 INTEGER NOT NULL DEFAULT 1,
    allow_hub_routing   INTEGER NOT NULL DEFAULT 1,
    hub_place_ids       TEXT,   -- JSON array of place ids; NULL = auto-discover (§8)
    max_hops            INTEGER NOT NULL DEFAULT 1,
    max_transfer_min    INTEGER,
    max_total_hours     REAL,
    poll_interval_min   INTEGER NOT NULL DEFAULT 60,   -- free-entry, floor enforced in code (§6)
    active              INTEGER NOT NULL DEFAULT 1,
    next_run_at         TEXT,
    last_run_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_routes_due ON routes(active, next_run_at);
CREATE INDEX idx_routes_user ON routes(user_id);

-- tier: discovery|confirmation
CREATE TABLE price_observations (
    id                  INTEGER PRIMARY KEY,
    route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('plane', 'train')),
    depart_date         TEXT NOT NULL,
    return_date         TEXT,
    price_minor         INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    itinerary_hash      TEXT NOT NULL,
    legs_json           TEXT NOT NULL,
    deep_link           TEXT,
    observed_at         TEXT NOT NULL,
    source_cached_at    TEXT,
    expires_at          TEXT,
    tier                TEXT NOT NULL CHECK (tier IN ('discovery', 'confirmation')),
    confidence          REAL NOT NULL
);
CREATE INDEX idx_obs_route_date ON price_observations(route_id, depart_date, observed_at DESC);

CREATE TABLE best_known (
    route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    source_id           TEXT NOT NULL,
    mode                TEXT NOT NULL,
    depart_date         TEXT NOT NULL,
    price_minor         INTEGER NOT NULL,
    itinerary_hash      TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    PRIMARY KEY (route_id, source_id, mode, depart_date)
);

-- stat_window: 30d|season|all
CREATE TABLE route_stats (
    route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    mode                TEXT NOT NULL,
    stat_window         TEXT NOT NULL,
    min_price_minor     INTEGER,
    min_price_at        TEXT,
    p25_price_minor     INTEGER,
    median_price_minor  INTEGER,
    sample_count        INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (route_id, mode, stat_window)
);

CREATE TABLE notifications_sent (
    id                  INTEGER PRIMARY KEY,
    route_id            INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    dedupe_key          TEXT NOT NULL UNIQUE,
    itinerary_hash      TEXT NOT NULL,
    price_minor         INTEGER NOT NULL,
    mode                TEXT NOT NULL,
    depart_date         TEXT NOT NULL,
    tier_at_send        TEXT NOT NULL,
    sent_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- status: ok|degraded|down
CREATE TABLE source_health (
    source_id           TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'ok',
    last_success_at      TEXT,
    last_error_at        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error_kind      TEXT,
    last_error_msg       TEXT,
    circuit_open_until   TEXT,
    requests_today       INTEGER NOT NULL DEFAULT 0,
    notified_admin_at    TEXT
);

-- kind: discovery|confirm|combo|health|notify ; state: queued|running|done|failed
-- 'notify' jobs are how quiet-hours deferral works (§9): the Notifier inserts one instead
-- of sending immediately, run_after = end of quiet hours, and the scheduler tick delivers
-- it like any other due job.
CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('discovery', 'confirm', 'combo', 'health', 'notify')),
    route_id            INTEGER REFERENCES routes(id) ON DELETE CASCADE,
    priority            INTEGER NOT NULL DEFAULT 0,
    payload_json        TEXT,
    state               TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    run_after           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at         TEXT
);
CREATE INDEX idx_jobs_pending ON jobs(state, priority DESC, run_after);

CREATE TABLE source_cache (
    cache_key           TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL,
    body                 BLOB NOT NULL,
    content_type         TEXT,
    fetched_at           TEXT NOT NULL,
    expires_at            TEXT NOT NULL
);
CREATE INDEX idx_source_cache_expiry ON source_cache(expires_at);

-- Backs /search (§10) — see §4 note on the 64-byte Telegram callback_data cap.
CREATE TABLE search_sessions (
    id                   TEXT PRIMARY KEY,   -- short base36
    user_id              INTEGER NOT NULL REFERENCES users(tg_user_id) ON DELETE CASCADE,
    query_json           TEXT NOT NULL,
    itineraries_json     TEXT,
    leg_pool_json        TEXT,
    hubs_json            TEXT,
    sources_meta_json    TEXT,
    manual_leg1_idx      INTEGER,
    manual_leg2_idx      INTEGER,
    manual_hub_place_id  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at            TEXT NOT NULL
);
CREATE INDEX idx_search_sessions_user ON search_sessions(user_id, created_at DESC);
CREATE INDEX idx_search_sessions_expiry ON search_sessions(expires_at);

INSERT INTO schema_migrations (version) VALUES (1);
