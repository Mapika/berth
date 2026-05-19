-- Capture the docker image's content-addressable id (`sha256:...`) at
-- container start so a deployment row is reproducible even if the upstream
-- tag is moved. `image_tag` (e.g. `vllm/vllm-openai:v0.20.2`) is a mutable
-- pointer; `image_digest` is what was actually run. Nullable: old rows and
-- rows whose engine never started have NULL.
ALTER TABLE deployments ADD COLUMN image_digest TEXT;
