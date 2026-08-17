-- Cache for JS-challenge-bootstrapped sessions (cookies/tokens), so a confirmation-tier
-- adapter like s7_direct doesn't have to solve the challenge on every search() call.
-- Cheap path first: reuse a still-valid session over curl_cffi. Only when that fails
-- (missing/expired/rejected) does the adapter fall back to a real browser solve.

CREATE TABLE source_sessions (
    source_id    TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,   -- cookies, headers, execution/conversation ids — adapter-specific shape
    obtained_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

INSERT INTO schema_migrations (version) VALUES (4);
