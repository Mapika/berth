-- Per-load plan history for base pre-warming.
-- The predictor v2.0 only pre-warmed adapters because rebuilding a base
-- deployment needs the operator's original `serve run` flags (gpu_ids,
-- ctx, max_loras, max_lora_rank, dtype, extra_args, etc). The deployments
-- table itself drops some of these (extra_args, gpu_memory_utilization)
-- and rotates as containers stop, so we capture the full DeploymentPlan
-- as JSON at load() time and mark it `reached_ready_at` only when the
-- engine actually became healthy. Predictor mines `MAX(reached_ready_at)`
-- per model to reconstruct a plan it can hand back to manager.load().
-- Companion design: docs/design/specs/2026-05-13-predictive-layer-design.md

CREATE TABLE IF NOT EXISTS deployment_plans (
    id                INTEGER PRIMARY KEY,
    model_id          INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    plan_json         TEXT NOT NULL,
    deployment_id     INTEGER REFERENCES deployments(id) ON DELETE SET NULL,
    reached_ready_at  TIMESTAMP,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Predictor lookup: most-recent successful plan per base.
CREATE INDEX IF NOT EXISTS idx_dep_plans_model_ready
    ON deployment_plans(model_id, reached_ready_at);
