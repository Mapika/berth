-- Service router direction: saved launch definitions for runnable
-- inference services. The first service family is model-serving
-- backends, so this table mirrors the DeploymentPlan fields needed to
-- launch vLLM/SGLang/TRT-LLM while keeping existing model/deployment
-- commands as a compatibility layer.

CREATE TABLE IF NOT EXISTS service_profiles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    model_name          TEXT NOT NULL,
    hf_repo             TEXT NOT NULL,
    revision            TEXT NOT NULL DEFAULT 'main',
    backend             TEXT NOT NULL,
    image_tag           TEXT NOT NULL,
    gpu_ids             TEXT NOT NULL DEFAULT '',
    tensor_parallel     INTEGER NOT NULL,
    max_model_len       INTEGER NOT NULL DEFAULT 8192,
    dtype               TEXT NOT NULL DEFAULT 'auto',
    pinned              INTEGER NOT NULL DEFAULT 0,
    idle_timeout_s      INTEGER,
    target_concurrency  INTEGER,
    max_loras           INTEGER NOT NULL DEFAULT 0,
    max_lora_rank       INTEGER NOT NULL DEFAULT 0,
    extra_args_json     TEXT NOT NULL DEFAULT '{}',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_service_profiles_backend
    ON service_profiles(backend);
