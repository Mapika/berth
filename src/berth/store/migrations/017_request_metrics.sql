-- Per-request outcome + latency for the Overview dashboard's historical
-- latency/error views. Distinct from usage_events (the predictor's
-- served-request log): this captures ALL finalized requests, including
-- pre-dispatch failures (bad model, auth, no-ready-service, adapter errors).

CREATE TABLE IF NOT EXISTS request_metrics (
    id              INTEGER PRIMARY KEY,
    ts              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model_name      TEXT,
    route_name      TEXT,
    deployment_id   INTEGER,
    backend         TEXT,
    status_code     INTEGER,
    is_error        INTEGER NOT NULL DEFAULT 0,
    dispatched      INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    ttft_ms         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rm_ts ON request_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_rm_model_ts ON request_metrics(model_name, ts);

-- Hourly rollup with exact per-bucket percentiles (computed from raw rows at
-- rollup time). model_name NULL = the all-models aggregate row for that hour.
CREATE TABLE IF NOT EXISTS request_metrics_hourly (
    id                INTEGER PRIMARY KEY,
    bucket_start      TIMESTAMP NOT NULL,
    model_name        TEXT,
    count             INTEGER NOT NULL,
    error_count       INTEGER NOT NULL,
    dispatched_count  INTEGER NOT NULL,
    latency_p50_ms    INTEGER,
    latency_p95_ms    INTEGER,
    ttft_p50_ms       INTEGER,
    ttft_p95_ms       INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rmh_bucket_model
    ON request_metrics_hourly(bucket_start, COALESCE(model_name, ''));
CREATE INDEX IF NOT EXISTS idx_rmh_bucket ON request_metrics_hourly(bucket_start);
