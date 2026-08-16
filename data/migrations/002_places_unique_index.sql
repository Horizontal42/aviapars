-- Needed to make data/seed_places.py idempotent (re-runnable to refresh data)
-- once it pulls the real Travelpayouts cities/airports dump instead of a
-- hardcoded handful of rows. Partial + compound: a single-airport city's
-- city code and its airport's code are often identical (e.g. Penza = PEZ for
-- both kind='city' and kind='airport'), so uniqueness must be scoped to
-- (kind, iata), not iata alone. Rows without an iata (future rail_station
-- entries keyed by yandex_code) are untouched by this index.

CREATE UNIQUE INDEX ux_places_kind_iata ON places(kind, iata) WHERE iata IS NOT NULL;

INSERT INTO schema_migrations (version) VALUES (2);
