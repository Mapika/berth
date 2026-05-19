-- Plan 04: API keys + per-key usage events.

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                           -- human-readable label
    prefix TEXT NOT NULL,                         -- "sk-aBc..." first 12 chars, for listing
    key_hash TEXT NOT NULL UNIQUE,                -- sha256 of full secret
    tier TEXT NOT NULL DEFAULT 'standard',
    -- Optional per-key overrides (NULL -> use tier defaults)
    rpm_override INTEGER,
    tpm_override INTEGER,
    rpd_override INTEGER,
    tpd_override INTEGER,
    rph_override INTEGER,
    tph_override INTEGER,
    rpw_override INTEGER,
    tpw_override INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP,
    last_used_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_revoked ON api_keys(revoked_at);

CREATE TABLE IF NOT EXISTS key_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    model_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_key_usage_events_key_ts ON key_usage_events(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_key_usage_events_ts ON key_usage_events(ts);
