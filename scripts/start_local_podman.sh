#!/usr/bin/env bash
set -euo pipefail

# ── What changed ──────────────────────────────────────────────────────────
#
# dev-lms's container command is no longer `sleep infinity`. It now runs
# docker/local/entrypoint.sh, which:
#   1. Runs scripts/bootstrap_lms.sh (idempotent — site creation, app
#      install, settings seeding, rag_service venv).
#   2. Starts `bench start` + the RAG consumer + the LMS submission consumer.
#
# That means a plain:
#   podman-compose --env-file env.local -f docker/local/docker-compose.local.yml up -d --build
# now brings up a FULLY working stack on its own — this script is just a
# thin, friendly wrapper around that, plus a readiness check.
#
# The rag_service isolated venv is also no longer rebuilt on every run: it
# lives in the `rag-venv-data` named volume and bootstrap_lms.sh only
# reinstalls it when rag_service/requirements.txt actually changes (tracked
# via a checksum file inside that volume).
# ─────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$PWD}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/local/docker-compose.local.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/env.local}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env.local. Copy .env.example to env.local and fill in the required values."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}"

echo "Starting all services (first run will be slow: bench init, site creation,"
echo "and the rag_service venv install; subsequent runs skip all of that)..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build

_wait_for_port() {
  local name=$1 port=$2
  for i in $(seq 1 60); do
    if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('localhost', $port)); s.close()" 2>/dev/null; then
      echo "  $name is ready (port $port)."
      return 0
    fi
    echo "  Waiting for $name on port $port... ($i/60)"
    sleep 5
  done
  echo "  WARNING: $name did not become ready in time — check: podman-compose -f \"$COMPOSE_FILE\" logs dev-lms"
}

echo "Waiting for services to be ready (bootstrap runs inside dev-lms first)..."
_wait_for_port "glific-stub" "${GLIFIC_STUB_PORT:-4000}"
_wait_for_port "llm-stub"    "${LLM_STUB_PORT:-8001}"
_wait_for_port "tap_plg_api" "${TAP_PLG_API_PORT:-8080}"

echo "Waiting for Plagiarism Database to be ready..."
until podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T tap_plg_postgres pg_isready -U postgres -d plagiarism_db >/dev/null 2>&1; do
  echo "  Waiting for postgres..."
  sleep 2
done

echo "Initializing Plagiarism Database (safe to re-run, uses CREATE TABLE IF NOT EXISTS)..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T tap_plg_postgres \
  psql -U postgres -d plagiarism_db < "$ROOT_DIR/../tap_plg/database/init.sql"

# This ensures the stubs are healthy, allowing dev-lms to successfully finish bootstrapping and launch bench start.
echo "Waiting for Frappe application framework to finish bootstrapping..."
_wait_for_port "Frappe (dev-lms)" "${WEB_PORT:-8000}"

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Local TAP LMS testbed is coming up. Use the commands below to check the logs for status!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Frappe LMS Service (tap_lms)    ->  http://${SITE_NAME}:${WEB_PORT:-8000}
  Frappe RAG Service (rag)        ->  http://${RAG_SITE_NAME}:${WEB_PORT:-8000}
  tap_plg API (Real ML Service)   ->  http://localhost:${TAP_PLG_API_PORT:-8080}
  RabbitMQ management UI          ->  http://localhost:15672  (guest / guest)
  LLM stub                        ->  http://localhost:${LLM_STUB_PORT:-8001}
  Glific stub                     ->  http://localhost:${GLIFIC_STUB_PORT:-4000}

  Admin user:     Administrator
  Admin password: ${ADMIN_PASSWORD:-admin}

bench, the RAG consumer, and the LMS submission consumer are already
running inside dev-lms. To watch their combined logs:

  podman-compose --env-file ./frappe_tap/env.local -f ./frappe_tap/docker/local/docker-compose.local.yml logs -f dev-lms

To re-run the bootstrap by hand (e.g. after editing rag_service/requirements.txt):

  podman-compose --env-file ./frappe_tap/env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
    exec dev-lms bash /workspace/frappe_tap/scripts/bootstrap_lms.sh

Next, send a test submission:

  curl -v -X POST "http://${SITE_NAME}:${WEB_PORT:-8000}/api/method/tap_lms.imgana.submission.assignment_submission" \\
    -H "Content-Type: application/json" \\
    -H "Authorization: token ${LOCAL_API_KEY:-local-dev-api-key-001}:${LOCAL_API_SECRET:-local-secret-key}" \\
    -d '{
        "api_key":   "${LOCAL_API_KEY:-local-dev-api-key-001}",
        "assign_id": "MockAssign-Basic",
        "name1":     "LocalDevStudent",
        "glific_id": "LOCAL_GLIFIC_001",
        "submission": "https://picsum.photos/200/300"
    }'

EOF
