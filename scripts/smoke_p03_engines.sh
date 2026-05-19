#!/usr/bin/env bash
set -euo pipefail

# Plan 03 smoke: same model, two engines back-to-back.
# Prereqs same as smoke_e2e.sh + lmsysorg/sglang:v0.5.11 image cached
# (or pulled on first use — that takes ~3-5 min).

cleanup() {
    berth stop 2>/dev/null || true
    berth daemon stop 2>/dev/null || true
}
trap cleanup EXIT

berth daemon start
berth pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b

for engine in vllm sglang; do
    echo "=== loading qwen-0_5b on $engine ==="
    berth run qwen-0_5b --gpu 0 --ctx 4096 --engine "$engine"
    sleep 2
    curl -sS "http://127.0.0.1:11500/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"qwen-0_5b\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply OK\"}],\"max_tokens\":8,\"stream\":false}" \
      | tee "/tmp/serve_p03_$engine.out"
    echo
    grep -q "OK" "/tmp/serve_p03_$engine.out" || { echo "FAIL: no OK from $engine"; exit 1; }
    berth stop
    sleep 2
done

echo "PASS"
