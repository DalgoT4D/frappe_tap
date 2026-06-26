#!/usr/bin/env bash
# This is the `command:` for the dev-lms service (replaces `sleep infinity`).
# It runs the bootstrap (idempotent — fast on every restart after the first),
# then starts bench + the two consumers so `podman-compose up` alone is enough.
set -euo pipefail

TIMEOUT=30
COUNTER=0
WAIT_TIME=2

echo "── Waiting for PostgreSQL to accept connections ──"
until python3 -c "import socket; s = socket.socket(); s.settimeout(1); s.connect(('postgres', 5432))" 2>/dev/null; do
  if [ "$COUNTER" -ge "$TIMEOUT" ]; then
      echo "❌ ERROR: PostgreSQL did not become available within ${TIMEOUT} seconds. Aborting."
      exit 1
  fi
  echo "PostgreSQL is unavailable - sleeping (${COUNTER}/${TIMEOUT}s)"
  COUNTER=$((COUNTER + WAIT_TIME))
  sleep $WAIT_TIME
done
echo "--- PostgreSQL is up! ---"

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}"

echo "── Running bootstrap (idempotent: skips anything already done) ──"
bash /workspace/frappe_tap/scripts/bootstrap_lms.sh

cd /home/frappe/frappe-bench

echo "── Starting bench, RAG consumer, and LMS submission consumer ──"

bench start &
BENCH_PID=$!

(
  cd sites
  SITE_NAME="$RAG_SITE_NAME" ../env/bin/python -c "import rag_service.scripts.console_consumer as cc; cc.run()"
) &
RAG_CONSUMER_PID=$!

(
  cd sites
  ../env/bin/python ../apps/tap_lms/scripts/console_consumer.py
) &
LMS_CONSUMER_PID=$!

cleanup() {
  echo "Shutting down..."
  kill "$BENCH_PID" "$RAG_CONSUMER_PID" "$LMS_CONSUMER_PID" 2>/dev/null || true
  wait
}
trap cleanup SIGTERM SIGINT

# If any one of these three dies, bring the whole container down so
# `podman-compose ps` / restart policies reflect real health instead of
# silently running in a half-broken state.
echo "── Workers active. Waiting for system runtime... ──"
wait "$BENCH_PID" # "$RAG_CONSUMER_PID" "$LMS_CONSUMER_PID"
EXIT_CODE=$?
echo "One of bench/consumers exited (code $EXIT_CODE) — stopping container."
cleanup
exit "$EXIT_CODE"
