CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    hf_repo TEXT NOT NULL,
    revision TEXT NOT NULL DEFAULT 'main',
    local_path TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    image_tag TEXT NOT NULL,
    gpu_ids TEXT NOT NULL DEFAULT '',  -- comma-separated GPU device IDs, e.g. '0,1'
    tensor_parallel INTEGER NOT NULL DEFAULT 1,
    max_model_len INTEGER,
    dtype TEXT NOT NULL DEFAULT 'auto',
    container_id TEXT,
    container_name TEXT,
    container_port INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','loading','ready','stopping','stopped','failed')),
    last_error TEXT,
    started_at TIMESTAMP,
    last_request_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status);
