-- Explicit router table. The first route family matches the OpenAI
-- request body's `model` field and maps it to a service profile.

CREATE TABLE IF NOT EXISTS service_routes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL UNIQUE,
    match_model          TEXT NOT NULL,
    profile_id           INTEGER NOT NULL
        REFERENCES service_profiles(id) ON DELETE CASCADE,
    fallback_profile_id  INTEGER
        REFERENCES service_profiles(id) ON DELETE SET NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    priority             INTEGER NOT NULL DEFAULT 100,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_service_routes_match_enabled
    ON service_routes(match_model, enabled, priority);
