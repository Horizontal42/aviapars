-- Flexible dates, part 2. 001 already had date_mode/fixed_dates/window_*_days, but only a
-- rolling anchor (offset from today). An explicit user range («с 10.09 по 20.09») must NOT
-- slide forward every night, so absolute windows get their own column; window_start_offset_days
-- stays for the rolling case («любая дата в ближайший месяц»). Exactly one of the two is set.
ALTER TABLE routes ADD COLUMN window_start_date TEXT;

-- Round-robin position through a flexible window for one-request-per-date sources (§6):
-- each cycle probes the next slice instead of re-querying 30 dates every time.
ALTER TABLE routes ADD COLUMN date_cursor INTEGER NOT NULL DEFAULT 0;
