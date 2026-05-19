#!/usr/bin/env bash
set -euo pipefail

# Plan 04 smoke: create a key, hit /v1/ with and without it, observe
# rate limits.
#
# Prereqs same as Plan 02 smoke (Docker + nvidia-container-toolkit + GPU).

cleanup() {
    berth stop 2>/dev/null || true
    berth daemon stop 2>/dev/null || true
}
trap cleanup EXIT

berth daemon start
berth pull Qwen/Qwen2.5-0.5B-Instruct --name qwen-0_5b
berth run qwen-0_5b --gpu 0 --ctx 4096

# Phase 1: no keys yet → auth bypassed
echo "=== phase 1: no keys, expect 200 ==="
code=$(curl -sS -o /dev/null -w "%{http_code}" \
  -X POST http://127.0.0.1:11500/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-0_5b","messages":[{"role":"user","content":"hi"}],"max_tokens":4}')
echo "HTTP $code"
test "$code" = "200" || { echo "FAIL: expected 200 (auth bypass)"; exit 1; }

# Phase 2: create a trial-tier key (RPM=10)
echo "=== phase 2: create trial key ==="
secret=$(berth key create alice --tier trial | awk '/^secret:/ {print $2}')
test -n "$secret" || { echo "no secret returned"; exit 1; }
echo "Got secret: ${secret:0:12}..."

# Phase 3: hit without bearer → expect 401
echo "=== phase 3: no bearer, expect 401 ==="
code=$(curl -sS -o /dev/null -w "%{http_code}" \
  -X POST http://127.0.0.1:11500/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-0_5b","messages":[{"role":"user","content":"hi"}],"max_tokens":4}')
echo "HTTP $code"
test "$code" = "401" || { echo "FAIL: expected 401, got $code"; exit 1; }

# Phase 4: hit with good bearer → expect 200
echo "=== phase 4: good bearer, expect 200 ==="
code=$(curl -sS -o /dev/null -w "%{http_code}" \
  -X POST http://127.0.0.1:11500/v1/chat/completions \
  -H "Authorization: Bearer $secret" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-0_5b","messages":[{"role":"user","content":"hi"}],"max_tokens":4}')
echo "HTTP $code"
test "$code" = "200" || { echo "FAIL: expected 200, got $code"; exit 1; }

# Phase 5: fire 15 requests rapidly → trial tier RPM=10, expect ≥ 1 429
echo "=== phase 5: 15 rapid requests, expect at least one 429 ==="
hits_429=0
for i in $(seq 1 15); do
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
      -X POST http://127.0.0.1:11500/v1/chat/completions \
      -H "Authorization: Bearer $secret" \
      -H 'Content-Type: application/json' \
      -d '{"model":"qwen-0_5b","messages":[{"role":"user","content":"hi"}],"max_tokens":1}')
    echo "  req $i → $code"
    if [ "$code" = "429" ]; then hits_429=$((hits_429+1)); fi
done
test $hits_429 -ge 1 || { echo "FAIL: expected at least one 429"; exit 1; }

echo "PASS ($hits_429 / 15 throttled)"
