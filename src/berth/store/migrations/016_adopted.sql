-- Adopted deployments wrap an externally-hosted OpenAI-compatible endpoint
-- that berth routes to but never launches/stops. 'managed' = berth owns the
-- container's lifecycle (the default for every existing row).
ALTER TABLE deployments ADD COLUMN source TEXT NOT NULL DEFAULT 'managed';
