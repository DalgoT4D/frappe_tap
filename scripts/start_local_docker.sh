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

if [[ ! -d "sites/$SITE_NAME" ]]; then
  bench new-site "$SITE_NAME" \
    --db-type postgres \
    --db-host postgres \
    --db-port 5432 \
    --db-root-username "$POSTGRES_USER" \
    --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
fi

# add the rag_service to the apps.txt file else bench new-site command will fail
echo "frappe
tap_lms
business_theme_v14
rag_service" > apps.txt

echo "frappe
tap_lms
business_theme_v14
rag_service" > sites/apps.txt

# Run migrations and explicit builds now that manifest maps are established
bench --site "$SITE_NAME" install-app tap_lms
bench --site "$SITE_NAME" install-app business_theme_v14
bench --site "$SITE_NAME" install-app rag_service
bench --site "$SITE_NAME" migrate

bench build --app tap_lms
bench build --app business_theme_v14

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

# ── RAG Settings → tap_lms site ───────────────────────────────────────────────
echo "Seeding RAG Settings..."
set_single_value "RAG Settings" base_url                    "http://${SITE_NAME}:${WEB_PORT:-8000}"
set_single_value "RAG Settings" assignment_context_endpoint "api/method/tap_lms.imgana.submission.get_assignment_context"
set_single_value "RAG Settings" student_context_endpoint    "api/method/tap_lms.imgana.submission.get_student_details"
set_single_value "RAG Settings" enable_caching              "0"

bench --site "$SITE_NAME" clear-cache
' # <--- This ends the massive Step 3 single-quoted container block cleanly!

# ── Step 4: Create separate venv & bridge for rag_service due to dependency conflicts ────────────────
echo "Setting up rag_service isolated venv..."

# single-quoted delimiter around 'OUTEREOF' heredoc below means:
# No variable expansion inside the heredoc (the $ signs are safe)
# No quote conflicts with the surrounding bash single-quote block
# The Python code itself can use any quotes freely

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash << 'OUTEREOF'
set -euo pipefail

# Create isolated venv for rag_service dependencies
python3 -m venv /home/frappe/rag_venv
/home/frappe/rag_venv/bin/pip install --no-cache-dir \
  -r /workspace/rag_service/requirements.txt

# Bridge the rag venv into Frappe's venv via a .pth file
cd /home/frappe/frappe-bench
./env/bin/python3 - << PYEOF
import site, os, sys
pth_dir = site.getsitepackages()[0]
py_ver = "python{}.{}".format(sys.version_info.major, sys.version_info.minor)
rag_site = "/home/frappe/rag_venv/lib/{}/site-packages".format(py_ver)
pth_file = os.path.join(pth_dir, "rag_isolated.pth")
open(pth_file, "w").write(rag_site + "\n")
print("Created .pth bridge: {} -> {}".format(pth_file, rag_site))
PYEOF

# Verify the bridge works
./env/bin/python3 -c "from rag_service.utils.rabbitmq_consumer import RabbitMQConsumer; print('rag_service import OK')"
OUTEREOF

echo "rag_service venv bridge complete."

# ── Step 5: Seed LLM Settings & RAG Secrets ───────────────────────────────────
echo "Seeding LLM Settings & RAG Secrets..."

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash << EOF
set -euo pipefail
cd /home/frappe/frappe-bench/sites
../env/bin/python3 - << 'PYEOF'
import frappe
frappe.init(site="$SITE_NAME")
frappe.connect()

# 1. LLM Settings (Stub)
if not frappe.db.exists("LLM Settings", {"provider": "Stub"}):
    doc = frappe.new_doc("LLM Settings")
    doc.provider = "Stub"
    doc.model_name = "${LLM_MODEL_NAME:-stub-gpt-4}"
    doc.base_url = "${LLM_BASE_URL:-http://llm-stub:8001}"
    doc.api_key = "${LLM_API_KEY:-local-stub-key}"
    doc.is_active = 1
    doc.insert()
    print("✓ Created Stub LLM Settings")
else:
    doc = frappe.get_doc("LLM Settings", {"provider": "Stub"})
    doc.model_name = "${LLM_MODEL_NAME:-stub-gpt-4}"
    doc.base_url = "${LLM_BASE_URL:-http://llm-stub:8001}"
    doc.is_active = 1
    doc.save()
    print("✓ Updated Stub LLM Settings")

# 2. RAG Settings Secret
frappe.db.set_value("RAG Settings", "RAG Settings", "api_secret", "local-secret-key")
print("✓ Seeded RAG Settings api_secret")

frappe.db.commit()
PYEOF
EOF

# ── Step 6: Create seed data ──────────────────────────────────────────────────────────
echo "Running seed_local.py..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash -lc '
  cd /home/frappe/frappe-bench/sites && \
  ../env/bin/python3 -c "import frappe; frappe.init(\"tap_lms.localhost\"); frappe.connect(); import sys; sys.path.insert(0, \"/workspace/frappe_tap\"); import scripts.seed_local"
'
cat <<EOF

# Note: tap_plg_stub was started in Step 1 & 2 — no extra step needed.

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
     exec dev-lms bash -lc "cd /home/frappe/frappe-bench/sites/ && ../env/bin/python -c \"import frappe; frappe.init('tap_lms.localhost'); frappe.connect(); import rag_service.scripts.console_consumer as cc; cc.run()\""

2a. Start the feedback consumer:
    podman-compose --env-file env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
    exec dev-lms bash -lc "cd /home/frappe/frappe-bench/sites/ && ../env/bin/python ../apps/tap_lms/scripts/console_consumer.py"

3. Check if test API key is successfully created and associated with Administrator in Frappe bench. If not, create a test API key:
   Frappe desk → API Key → New → key: local-test-key-001 → Save

4. Send a test submission (ensure auth token matches one declared in seed script or one created above manually):
    curl -v -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.imgana.submission.submit_artwork" -H "Content-Type: application/json" -H "Authorization: token local-dev-api-key-001:local-secret-key" -d '{
        "api_key":   "local-dev-api-key-001",
        "assign_id": "MockAssign-Basic",
        "name1":     "LocalDevStudent",
        "glific_id": "LOCAL_GLIFIC_001",
        "img_url":   "https://picsum.photos/200/300"
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
