-- Bounded-storage rollup of usage_events.
--
-- After retention_days, the rollup job aggregates raw events into this
-- table by (base_name, adapter_name, hour_of_week) and deletes the raw
-- rows. The predictor's time-of-day rule still reads usage_events for
-- recent history; longer-term patterns can layer on top of this table
-- in a future release.
CREATE TABLE IF NOT EXISTS usage_aggregates (
    id              INTEGER PRIMARY KEY,
    base_name       TEXT NOT NULL,
    adapter_name    TEXT,                       -- NULL = bare base
    hour_of_week    INTEGER NOT NULL,           -- 0..167
    count           INTEGER NOT NULL DEFAULT 0,
    last_rollup_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Composite uniqueness so the rollup can upsert per
-- (base, adapter, hour-of-week). Adapter is NULLable, so the index
-- includes COALESCE to give NULL adapter a stable slot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_aggregates_unique
    ON usage_aggregates(base_name, COALESCE(adapter_name, ''), hour_of_week);
CREATE INDEX IF NOT EXISTS idx_usage_aggregates_base_hour
    ON usage_aggregates(base_name, hour_of_week);
