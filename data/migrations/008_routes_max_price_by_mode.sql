-- Optional per-mode price ceilings, for a route that watches more than one kind of transport
-- ("уведомь на самолёт дешевле 8000 ИЛИ на поезд дешевле 4000"). Purely additive: NULL means
-- "one shared ceiling", which is what `max_price` has always been and stays for every existing
-- route, every single-mode route, and every multi-mode route whose owner didn't want to bother
-- splitting it. Nothing reads this column unless the user opted in.
--
-- Shape mirrors the precedent `mode_pref` already sets (see domain/places.allowed_modes): a JSON
-- value keyed by mode string, so a third mode (bus, §5) needs no further schema change — only a
-- new key. Values are minor units, same as `max_price`, and share `max_price_currency`; a
-- separate currency per mode would be a different feature (nobody tracks one route in two
-- currencies) and is deliberately not modelled.
--
-- `max_price` stays NOT NULL and meaningful even when this is populated: it holds the larger of
-- the per-mode ceilings and is what a *mixed*-mode hub itinerary (one plane leg, one train leg)
-- is compared against, since such an itinerary's price is neither a plane price nor a train
-- price — see domain/dealer.threshold_for for the reasoning.
ALTER TABLE routes ADD COLUMN max_price_by_mode TEXT;  -- JSON {"plane": 800000, "train": 400000}

INSERT INTO schema_migrations (version) VALUES (8);
