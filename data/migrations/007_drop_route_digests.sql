-- Redesign of digest mode (006): that version staged only the *cheapest* itinerary seen
-- in a day, overwriting any earlier qualifying find — so two separate deals the same day
-- collapsed into one message. The user's actual requirement is different: every deal that
-- would have fired in instant mode should still fire, just all delivered together at the
-- daily anchor instead of one by one throughout the day. That is exactly what the
-- quiet-hours deferral already does (render the card now, enqueue it, deliver later) —
-- digest mode is reimplemented as "always defer to the next quiet_hours_end", reusing the
-- existing `jobs` table, so this dedicated best-of-day accumulator is no longer needed.
DROP TABLE route_digests;

INSERT INTO schema_migrations (version) VALUES (7);
