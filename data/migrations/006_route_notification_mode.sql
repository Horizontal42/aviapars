-- Per-route choice of *when* a qualifying price becomes a message:
--   'instant' — today's behaviour: notify as soon as a price crosses the threshold.
--   'digest'  — at most one message a day, carrying the best qualifying price of that day.
-- Default 'instant' so existing routes keep behaving exactly as they did.
ALTER TABLE routes ADD COLUMN notification_mode TEXT NOT NULL DEFAULT 'instant'
    CHECK (notification_mode IN ('instant', 'digest'));

-- Accumulator for digest mode: one row per route holding the best qualifying itinerary seen
-- so far in the current digest window, already rendered.
--
-- Deliberately NOT derived from price_observations at send time, even though that table
-- already records every offer of every cycle and its 90-day retention would easily outlive a
-- one-day digest. The shape is wrong, not the retention: price_observations stores individual
-- *offers*, while what crosses routes.max_price is a whole *itinerary* — for a hub route that
-- is two offers plus a transfer cost, a number no single observation row carries. Picking
-- "cheapest observation under the threshold" would therefore both invent deals (a single leg
-- of a composed trip is naturally cheaper than the trip) and miss real ones (a qualifying
-- hub itinerary whose legs are individually above the threshold). It also has no idea whether
-- an itinerary was confirmation-tier. Staging the decided itinerary is the honest thing.
--
-- pending_day / last_sent_day are the user's *local digest day*, not a UTC date: the window
-- runs quiet_hours_end -> quiet_hours_end (see notify/notifier.digest_day), which is also why
-- a digest can never land inside the user's quiet hours.
CREATE TABLE route_digests (
    route_id        INTEGER PRIMARY KEY REFERENCES routes(id) ON DELETE CASCADE,
    pending_day     TEXT,       -- local digest day the staged best belongs to; NULL = nothing staged
    price_minor     INTEGER,
    currency        TEXT,
    itinerary_hash  TEXT,
    mode            TEXT,
    depart_date     TEXT,
    tier_at_send    TEXT,
    card_text       TEXT,       -- rendered at stage time, same as a quiet-hours notify job's payload
    staged_at       TEXT,
    last_sent_day   TEXT        -- local digest day of the last digest claimed for delivery
);

INSERT INTO schema_migrations (version) VALUES (6);
