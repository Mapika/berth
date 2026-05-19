-- Adapter registry and per-deployment adapter state.
-- Adapters (LoRA / DoRA) are first-class entities tied to a base model.
-- Junction table tracks which adapters are loaded into which deployments.
-- New columns on `deployments` track LoRA capacity. Federation-ready
-- columns (source_peer_id, updated_at) are reserved for future sync.

CREATE TABLE IF NOT EXISTS adapters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    base_model_id   INTEGER NOT NULL REFERENCES models(id),
    hf_repo         TEXT NOT NULL,
    revision        TEXT NOT NULL DEFAULT 'main',
    local_path      TEXT,
    size_mb         INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Future peer sync fields:
    source_peer_id  TEXT,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_adapters_base_model_id ON adapters(base_model_id);
CREATE INDEX IF NOT EXISTS idx_adapters_updated_at ON adapters(updated_at);

CREATE TABLE IF NOT EXISTS deployment_adapters (
    deployment_id   INTEGER NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    adapter_id      INTEGER NOT NULL REFERENCES adapters(id),
    loaded_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (deployment_id, adapter_id)
);

CREATE INDEX IF NOT EXISTS idx_dep_adapters_last_used
    ON deployment_adapters(deployment_id, last_used_at);

ALTER TABLE deployments ADD COLUMN max_loras INTEGER NOT NULL DEFAULT 0;
