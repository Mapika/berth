-- Optional per-key model allowlist.
-- JSON-encoded list of model names, or NULL = no restriction (default).
-- Empty list [] = restrict-all (zero allowed).
ALTER TABLE api_keys ADD COLUMN allowed_models TEXT;
