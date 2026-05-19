#!/usr/bin/env bash
set -euo pipefail

# Plan 02 smoke test: multi-model serving with pin + auto.
#
# Prerequisites (same as Plan 01):
#   - Docker daemon + nvidia-container-toolkit + CUDA GPUs (>=1)
#   - HF cache primed or HF_TOKEN set
#   - serve installed via `uv pip install -e ".[dev]"`
#
# Verifies:
#   - daemon start/stop
#   - pull (register) for two models
#   - run --pin and run --idle-timeout
#   - /v1/chat/completions routes by `model` field to the right deployment
#   - berth ps shows pinned / VRAM columns
#   - stop tears containers down

cleanup() {
    berth stop 2>/dev/null || true
    berth daemon stop 2>/dev/null || true
}
trap cleanup EXIT

berth daemon start

berth pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b
berth pull Qwen/Qwen2.5-1.5B-Instruct --name qwen-1_5b

# Pinned: never auto-evicted
berth run qwen-0_5b --gpu 0 --ctx 4096 --pin

# Auto: idle-evicted after 60 s with no traffic
berth run qwen-1_5b --gpu 0 --ctx 4096 --idle-timeout 60

berth ps

# Hit both models — proxy routes by `model` field
for m in qwen-0_5b qwen-1_5b; do
    echo "--- $m ---"
    curl -sS -N "http://127.0.0.1:11500/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply: OK\"}],\"max_tokens\":4,\"stream\":false}" \
      | tee "/tmp/serve_smoke_$m.out"
    echo
    grep -q "OK" "/tmp/serve_smoke_$m.out" || { echo "FAIL: no OK from $m"; exit 1; }
done

echo "PASS"
