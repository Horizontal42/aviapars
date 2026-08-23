-- When the daily digest lands, as a user-local "HH:MM". NULL = "at the end of quiet hours",
-- which is exactly what every digest did before this column existed, so nothing changes for
-- an existing user until they set one.
--
-- Split from quiet_hours_end rather than reusing it, after the owner asked to make digest
-- time configurable. The two were one field doing double duty, and the field is a trap once
-- it becomes user-facing: wanting the roundup at 20:00 would have meant setting
-- quiet_hours_end='20:00', i.e. also declaring 23:00-20:00 as "do not disturb" and
-- suppressing instant notifications for almost the whole day. They are genuinely different
-- questions — "when may the bot talk to me" against "when do I want the summary" — so they
-- are two fields now.
--
-- Quiet hours still win where they overlap: a digest_time inside the quiet window is
-- delivered at quiet_hours_end instead (see notify/notifier.next_digest_delivery). "Do not
-- disturb" is the stronger promise of the two, and a digest that wakes someone at 03:00
-- because they typed it in would be the bot breaking its own rule.
ALTER TABLE users ADD COLUMN digest_time TEXT;  -- 'HH:MM' local, NULL = quiet_hours_end

INSERT INTO schema_migrations (version) VALUES (9);
