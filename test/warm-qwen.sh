#!/usr/bin/env bash
# Probe the Qwen endpoint directly, bypassing the dashboard.
#
# Reads .env explicitly rather than trusting the ambient shell: ~/.zshenv
# exports a stale VLLM_API_KEY for the old vllm-secondbrain endpoint.
#
# A cold endpoint returns an immediate 503 (Modal's proxy does not hold the
# connection open while the container boots), so we poll until it serves.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a && . ./.env && set +a

URL="$VLLM_BASE_URL/chat/completions"
BODY=$(printf '{"model":"%s","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' "$VLLM_MODEL")
start=$(date +%s)

while :; do
  code=$(curl -s -m 120 -o /tmp/warm-qwen.out -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $VLLM_API_KEY" \
    -d "$BODY" "$URL")
  elapsed=$(( $(date +%s) - start ))

  case "$code" in
    200) echo "warm after ${elapsed}s"; cat /tmp/warm-qwen.out; echo; exit 0 ;;
    502|503|504) echo "[${elapsed}s] HTTP $code — still booting…" ;;
    *) echo "HTTP $code"; cat /tmp/warm-qwen.out; echo; exit 1 ;;
  esac

  [ "$elapsed" -gt "${VLLM_WARM_TIMEOUT:-720}" ] && { echo "gave up after ${elapsed}s"; exit 1; }
  sleep 5
done
