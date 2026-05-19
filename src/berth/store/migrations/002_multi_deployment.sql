-- Plan 02: multi-deployment lifecycle.
-- Add fields needed for pin/auto-swap, idle eviction, KV-aware placement,
-- and host-side routing addresses.

ALTER TABLE deployments ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deployments ADD COLUMN idle_timeout_s INTEGER;
ALTER TABLE deployments ADD COLUMN vram_reserved_mb INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deployments ADD COLUMN container_address TEXT;

CREATE INDEX IF NOT EXISTS idx_deployments_model_status ON deployments(model_id, status);
CREATE INDEX IF NOT EXISTS idx_deployments_last_request_at ON deployments(last_request_at);
