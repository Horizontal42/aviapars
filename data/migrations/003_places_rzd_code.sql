-- rzd_direct adapter (plans/squishy-scribbling-dawn.md §5 "Planned adapters — optional")
-- needs pass.rzd.ru's own numeric station/city codes (e.g. Moscow = 2000000), which are
-- neither the IATA code nor the Yandex Rasp `c<N>` code already on this table.

ALTER TABLE places ADD COLUMN rzd_code TEXT;

INSERT INTO schema_migrations (version) VALUES (3);
