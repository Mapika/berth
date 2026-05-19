-- Catch LoRA rank mismatches before they
-- become engine-side 502s.
--   `adapters.lora_rank`: extracted from the adapter's PEFT
--     adapter_config.json at pull/download time. NULL when the adapter
--     isn't PEFT-shaped or the config didn't expose `r`.
--   `deployments.max_lora_rank`: parsed from the operator's
--     `--max-lora-rank` --extra arg when the deployment was created.
--     0 = unset; the runtime treats 0 as "engine default (16)" so a
--     too-large adapter is caught even without an explicit override.
ALTER TABLE adapters ADD COLUMN lora_rank INTEGER;
ALTER TABLE deployments ADD COLUMN max_lora_rank INTEGER NOT NULL DEFAULT 0;
