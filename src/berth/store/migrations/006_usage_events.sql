-- Per-request usage events for predictive pre-warming.
-- Each request inserts one row at dispatch time; the predictor mines this
-- table for time-of-day, sequencing, and key-affinity patterns.
-- source_peer_id is NULL until peer sync exists.
-- Companion design: docs/design/specs/2026-05-13-predictive-layer-design.md

CREATE TABLE IF NOT EXISTS usage_events (
    id              INTEGER PRIMARY KEY,
    ts              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    api_key_id      INTEGER REFERENCES api_keys(id),
    model_name      TEXT NOT NULL,        -- as the client said it
    base_name       TEXT NOT NULL,        -- the resolved base
    adapter_name    TEXT,                 -- the resolved adapter, NULL if none
    deployment_id   INTEGER REFERENCES deployments(id),
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cold_loaded     INTEGER NOT NULL DEFAULT 0,
    -- Future peer sync fields:
    source_peer_id  TEXT
);

-- Predictor query patterns: by-time, by-base+time, by-key+time.
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_base_ts ON usage_events(base_name, ts);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_events(api_key_id, ts);
