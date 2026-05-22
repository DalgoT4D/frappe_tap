#!/usr/bin/env bash
set -euo pipefail

# ── Changes from original ─────────────────────────────────────────────────────
#
# 1. podman-compose up now also starts:
#      tap_plg_stub  — replaces tap_plg_worker + tap_plg_api in one container
#      llm-stub      — fake OpenAI/TogetherAI/VertexAI
#      glific-stub   — fake Glific WhatsApp API
#
# 2. After Frappe bench setup, two new DocTypes are seeded:
#      Glific Settings → api_url pointed at local glific-stub
#      LLM Settings    → provider/base_url pointed at local llm-stub
#
# 3. env.local needs these new variables:
#      GLIFIC_API_URL, GLIFIC_API_KEY
#      LLM_PROVIDER, LLM_MODEL_NAME, LLM_API_KEY, LLM_BASE_URL
#
# ─────────────────────────────────────────────────────────────────────────────

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
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

# ── Step 1: Start infrastructure + stubs ──────────────────────────────────────
echo "Starting infrastructure and stub services..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build \
  postgres \
  redis-cache \
  redis-queue \
  rabbitmq \
  tap_plg_stub \
  llm-stub \
  glific-stub

# Wait for stubs to be accepting connections before starting the dev container.
echo "Waiting for stub services to be ready..."

_wait_for_port() {
  local name=$1
  local port=$2
  for i in $(seq 1 20); do
    if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('localhost', $port)); s.close()" 2>/dev/null; then
      echo "  $name is ready (port $port)."
      return 0
    fi
    echo "  Waiting for $name on port $port... ($i/20)"
    sleep 3
  done
  echo "  WARNING: $name did not become ready in time — continuing anyway."
}

_wait_for_port "glific-stub"  "${GLIFIC_STUB_PORT:-4000}"
_wait_for_port "llm-stub"     "${LLM_STUB_PORT:-8001}"
_wait_for_port "tap_plg_stub" "${TAP_PLG_API_PORT:-8080}"

# ── Step 2: Start Frappe LMS & RAG dev containers ─────────────────────────────
echo "Starting Frappe LMS container..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build dev-lms

# ── FIX: TARGET 'dev-lms' EXPLICITLY INSTEAD OF THE OLD 'dev' VALUE ────────────
echo "Aligning environment storage volume tracking permissions..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev-lms chown -R frappe:frappe /home/frappe/frappe-bench
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev-lms chown -R frappe:frappe /workspace/frappe_tap/tap_lms/__pycache__ 2>/dev/null || true
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev-lms chmod -R a+rX /workspace/frappe_tap

# ── Step 3: Frappe bench setup (Targeting dev-lms specifically) ────────────────
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash -lc '
set -euo pipefail

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

if [[ ! -d /home/frappe/frappe-bench/apps/frappe ]]; then
  if [[ -n "$(find /home/frappe/frappe-bench -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "/home/frappe/frappe-bench is not empty but Frappe is missing."
    echo "If this is a broken local setup, reset it with:"
    echo "  podman-compose --env-file env.local -f docker/local/docker-compose.local.yml down -v"
    exit 1
  fi
  cd /home/frappe
  bench init \
    --frappe-branch "$FRAPPE_BRANCH" \
    --skip-redis-config-generation \
    --ignore-exist \
    frappe-bench
fi

cd /home/frappe/frappe-bench

bench set-config -g db_host postgres
bench set-config -g db_port 5432
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -g socketio_port 9000

if [[ ! -e apps/tap_lms ]]; then
  ln -s /workspace/frappe_tap apps/tap_lms
fi

if [[ ! -e apps/rag_service ]]; then
  ln -s /workspace/rag_service apps/rag_service
fi

./env/bin/python -m pip install -q --upgrade pip setuptools wheel flit_core
./env/bin/python -m pip install -q -e /workspace/frappe_tap --no-build-isolation
./env/bin/python -m pip install -q -e /workspace/rag_service --no-deps --no-build-isolation

if [[ ! -d apps/business_theme_v14 ]]; then
  bench get-app "$BUSINESS_THEME_REPO"
fi

bench build --app tap_lms
bench build --app business_theme_v14

if [[ ! -d "sites/$SITE_NAME" ]]; then
  bench new-site "$SITE_NAME" \
    --db-type postgres \
    --db-host postgres \
    --db-port 5432 \
    --db-root-username "$POSTGRES_USER" \
    --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --install-app tap_lms \
    --install-app business_theme_v14 \
    --install-app rag_service
else
  bench --site "$SITE_NAME" migrate
fi

if ! bench --site "$SITE_NAME" list-apps | grep -qx "business_theme_v14"; then
  bench --site "$SITE_NAME" install-app business_theme_v14
fi

bench --site "$SITE_NAME" set-config developer_mode 1
bench --site "$SITE_NAME" set-config host_name "http://${SITE_NAME}:${WEB_PORT:-8000}"

# ── Helper ────────────────────────────────────────────────────────────────────
set_single_value() {
  local doctype="$1"
  local field="$2"
  local value="${3:-}"
  local args
  args="$(python -c "import json,sys; print(json.dumps([sys.argv[1], sys.argv[2], sys.argv[3]]))" "$doctype" "$field" "$value")"
  bench --site "$SITE_NAME" execute frappe.db.set_single_value --args "$args"
}

# ── RabbitMQ Settings ─────────────────────────────────────────────────────────
if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  set_single_value "RabbitMQ Settings" host                     "${RABBITMQ_HOST:-}"
  set_single_value "RabbitMQ Settings" port                     "${RABBITMQ_PORT:-5672}"
  set_single_value "RabbitMQ Settings" virtual_host             "${RABBITMQ_VIRTUAL_HOST:-/}"
  set_single_value "RabbitMQ Settings" username                 "${RABBITMQ_USERNAME:-guest}"
  set_single_value "RabbitMQ Settings" password                 "${RABBITMQ_PASSWORD:-guest}"
  set_single_value "RabbitMQ Settings" submission_queue         "${RABBITMQ_SUBMISSION_QUEUE:-}"
  set_single_value "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
  set_single_value "RabbitMQ Settings" feedback_results_queue   "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
fi

# ── GCS Settings ──────────────────────────────────────────────────────────────
set_single_value "GCS Settings" enabled          "${GCS_ENABLED:-0}"
set_single_value "GCS Settings" bucket_name      "${GCS_BUCKET_NAME:-}"
set_single_value "GCS Settings" project_id       "${GCS_PROJECT_ID:-}"
set_single_value "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

# ── ElevenLabs Settings ───────────────────────────────────────────────────────
set_single_value "ElevenLabs Settings" enabled "${ELEVENLABS_ENABLED:-0}"
set_single_value "ElevenLabs Settings" api_key  "${ELEVENLABS_API_KEY:-disabled-local-placeholder}"

# ── VoiceAgentSettings ────────────────────────────────────────────────────────
set_single_value "VoiceAgentSettings" enabled                  "${VOICE_AGENT_ENABLED:-0}"
set_single_value "VoiceAgentSettings" service_url              "${VOICE_AGENT_SERVICE_URL:-}"
set_single_value "VoiceAgentSettings" client_id                "${VOICE_AGENT_CLIENT_ID:-}"
set_single_value "VoiceAgentSettings" client_secret            "${VOICE_AGENT_CLIENT_SECRET:-}"
set_single_value "VoiceAgentSettings" default_contact_group_id "${VOICE_AGENT_DEFAULT_CONTACT_GROUP_ID:-}"
set_single_value "VoiceAgentSettings" agent_id                 "${VOICE_AGENT_AGENT_ID:-}"
set_single_value "VoiceAgentSettings" auth_token_cache_ttl     "${VOICE_AGENT_AUTH_TOKEN_CACHE_TTL:-3600}"

# ── Glific Settings → glific-stub ─────────────────────────────────────────────
echo "Seeding Glific Settings → glific-stub..."
set_single_value "Glific Settings" api_url "${GLIFIC_API_URL:-http://glific-stub:4000}"
set_single_value "Glific Settings" api_key "${GLIFIC_API_KEY:-local-stub-key}"

# ── LLM Settings → llm-stub ───────────────────────────────────────────────────
echo "Seeding LLM Settings → llm-stub..."
set_single_value "LLM Settings" provider   "${LLM_PROVIDER:-openai}"
set_single_value "LLM Settings" model_name "${LLM_MODEL_NAME:-stub-gpt-4}"
set_single_value "LLM Settings" api_key    "${LLM_API_KEY:-local-stub-key}"
set_single_value "LLM Settings" base_url   "${LLM_BASE_URL:-http://llm-stub:8001}"
set_single_value "LLM Settings" is_active  "1"

echo "Seeding encrypted RAG Settings api_secret..."
bench --site "\$SITE_NAME" execute frappe.db.set_value --args "[\"RAG Settings\", \"RAG Settings\", \"api_secret\", \"local-secret-key\"]"

echo "Seeding RAG Settings API Endpoints..."
bench --site "\$SITE_NAME" execute frappe.db.set_value --args "[\"RAG Settings\", \"RAG Settings\", \"base_url\", \"http://localhost:8000\"]"
bench --site "\$SITE_NAME" execute frappe.db.set_value --args "[\"RAG Settings\", \"RAG Settings\", \"assignment_context_endpoint\", \"/api/method/tap_lms.api.get_assignment_context\"]"
bench --site "\$SITE_NAME" execute frappe.db.set_value --args "[\"RAG Settings\", \"RAG Settings\", \"student_context_endpoint\", \"/api/method/tap_lms.api.get_student_context\"]"

echo "Creating an isolated environment for RAG packages..."
python3 -m venv /home/frappe/rag_venv
/home/frappe/rag_venv/bin/pip install --no-cache-dir -r /workspace/rag_service/requirements.txt

# Ensure we are physically sitting inside the bench root folder
cd /home/frappe/frappe-bench
./env/bin/python -c "import site, os; p = os.path.join(site.getsitepackages()[0], \"rag_isolated.pth\"); open(p, \"w\").write(\"/home/frappe/rag_venv/lib/python3.14/site-packages\n\")"

bench --site "$SITE_NAME" clear-cache
' # <--- This ends the massive Step 3 single-quoted container block cleanly!

# Note: tap_plg_stub was started in Step 1 & 2 — no extra step needed.

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Local TAP LMS testbed is ready.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Frappe LMS Service (tap_lms)    →  http://${SITE_NAME}:${WEB_PORT:-8000}
  tap_plg stub (consumer + API)   →  http://localhost:${TAP_PLG_API_PORT:-8080}
  RabbitMQ management UI          →  http://localhost:15672  (guest / guest)
  LLM stub                        →  http://localhost:${LLM_STUB_PORT:-8001}
  Glific stub                     →  http://localhost:${GLIFIC_STUB_PORT:-4000}

  Admin user:     Administrator
  Admin password: ${ADMIN_PASSWORD}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Next steps:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Start the Frappe LMS web server:
   podman-compose --env-file env.local -f docker/local/docker-compose.local.yml \\
     exec dev-lms bash -lc "cd /home/frappe/frappe-bench && bench start"

2. Start your RAG Worker Consumer:
   podman-compose --env-file env.local -f docker/local/docker-compose.local.yml \\
     exec dev-lms bash -lc "cd /home/frappe/frappe-bench && ../env/bin/python -c \"import frappe; frappe.init('tap_lms.localhost'); frappe.connect(); import rag_service.scripts.console_consumer as cc; cc.run()\""

3. Create a test API key:
   Frappe desk → API Key → New → key: local-test-key-001 → Save

4. Send a test submission:
   curl -X POST \\
     "http://${SITE_NAME}:${WEB_PORT:-8000}/api/method/tap_lms.imgana.submission.submit_artwork" \\
     -H "Content-Type: application/json" \\
     -d '{
       "api_key": "local-test-key-001",
       "assign_id": "TEST-ASSIGN-001",
       "name1": "Test Student",
       "glific_id": "919999999999",
       "img_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png"
     }'

5. Verify the pipeline:
   # tap_plg_stub processed the submission
   curl http://localhost:${TAP_PLG_API_PORT:-8080}/stub/stats | python3 -m json.tool

   # Glific stub received the WhatsApp trigger
   curl http://localhost:${GLIFIC_STUB_PORT:-4000}/stub/flow-calls | python3 -m json.tool

6. Reset stub state between test runs:
   curl http://localhost:${GLIFIC_STUB_PORT:-4000}/stub/reset

7. Watch the full pipeline trace live:
   podman-compose --env-file env.local -f docker/local/docker-compose.local.yml logs -f

EOF
