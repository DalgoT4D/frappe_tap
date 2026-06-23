#!/usr/bin/env bash
set -euo pipefail

# ── Changes from original ─────────────────────────────────────────────────────
#
# 1. podman-compose up now also starts:
#      tap_plg_worker + tap_plg_api + tap_plg_postgres — real ML service
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

# rag_service runs as its OWN Frappe site / own database (separate from
# tap_lms), mirroring dev/prod — see frappe_tap/scripts/init.sh for details.
RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}"
RAG_POSTGRES_DB="${RAG_POSTGRES_DB:-rag_lms}"

# Single source of truth for local dev API credentials — reused below instead
# of repeating the literal strings in every Python block.
LOCAL_API_KEY="${LOCAL_API_KEY:-local-dev-api-key-001}"
LOCAL_API_SECRET="${LOCAL_API_SECRET:-local-secret-key}"

# ── Step 1: Start infrastructure + services ──────────────────────────────────
echo "Starting infrastructure and services..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build \
  postgres \
  redis-cache \
  redis-queue \
  rabbitmq \
  tap_plg_postgres \
  tap_plg_worker \
  tap_plg_api \
  llm-stub \
  glific-stub

# Wait for services to be ready before starting the dev container.
echo "Waiting for services to be ready..."

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
_wait_for_port "tap_plg_api"  "${TAP_PLG_API_PORT:-8080}"

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

# rag_service gets its own site / own database — separate from tap_lms.
RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}"
RAG_POSTGRES_DB="${RAG_POSTGRES_DB:-rag_lms}"

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

# apps.txt lists every app available in the bench (needed so `bench new-site`
# does not choke when either site installs rag_service) — it is NOT where
# per-site installation is decided, that happens via install-app below.
echo "frappe
tap_lms
business_theme_v14
rag_service" > apps.txt

echo "frappe
tap_lms
business_theme_v14
rag_service" > sites/apps.txt

# ── tap_lms site ──────────────────────────────────────────────────────────────
if [[ ! -d "sites/$SITE_NAME" ]]; then
  bench new-site "$SITE_NAME" \
    --db-type postgres \
    --db-host postgres \
    --db-port 5432 \
    --db-root-username "$POSTGRES_USER" \
    --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
fi

bench --site "$SITE_NAME" install-app tap_lms
bench --site "$SITE_NAME" install-app business_theme_v14
bench --site "$SITE_NAME" migrate

bench build --app tap_lms
bench build --app business_theme_v14

bench --site "$SITE_NAME" set-config developer_mode 1
bench --site "$SITE_NAME" set-config host_name "http://${SITE_NAME}:${WEB_PORT:-8000}"

# ── rag_service site (separate DB, same Postgres instance) ──────────────────
if [[ ! -d "sites/$RAG_SITE_NAME" ]]; then
  bench new-site "$RAG_SITE_NAME" \
    --db-type postgres \
    --db-host postgres \
    --db-port 5432 \
    --db-name "$RAG_POSTGRES_DB" \
    --db-root-username "$POSTGRES_USER" \
    --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
fi

bench --site "$RAG_SITE_NAME" install-app rag_service
bench --site "$RAG_SITE_NAME" migrate
bench --site "$RAG_SITE_NAME" set-config developer_mode 1

# ── Helper ────────────────────────────────────────────────────────────────────
# Takes the site as the first arg so it can target either site.
set_single_value() {
  local site="$1"
  local doctype="$2"
  local field="$3"
  local value="${4:-}"
  local args
  args="$(python -c "import json,sys; print(json.dumps([sys.argv[1], sys.argv[2], sys.argv[3]]))" "$doctype" "$field" "$value")"
  bench --site "$site" execute frappe.db.set_single_value --args "$args"
}

# ── tap_lms site settings ─────────────────────────────────────────────────────

# RabbitMQ Settings (tap_lms'"'"'s own copy — submission-queue producer)
if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  set_single_value "$SITE_NAME" "RabbitMQ Settings" host                     "${RABBITMQ_HOST:-}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" port                     "${RABBITMQ_PORT:-5672}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" virtual_host             "${RABBITMQ_VIRTUAL_HOST:-/}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" username                 "${RABBITMQ_USERNAME:-guest}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" password                 "${RABBITMQ_PASSWORD:-guest}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" submission_queue         "${RABBITMQ_SUBMISSION_QUEUE:-}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
  set_single_value "$SITE_NAME" "RabbitMQ Settings" feedback_results_queue   "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
fi

# GCS Settings (tap_lms'"'"'s own copy)
set_single_value "$SITE_NAME" "GCS Settings" enabled          "${GCS_ENABLED:-0}"
set_single_value "$SITE_NAME" "GCS Settings" bucket_name      "${GCS_BUCKET_NAME:-}"
set_single_value "$SITE_NAME" "GCS Settings" project_id       "${GCS_PROJECT_ID:-}"
set_single_value "$SITE_NAME" "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

# ElevenLabs Settings
set_single_value "$SITE_NAME" "ElevenLabs Settings" enabled "${ELEVENLABS_ENABLED:-0}"
set_single_value "$SITE_NAME" "ElevenLabs Settings" api_key  "${ELEVENLABS_API_KEY:-disabled-local-placeholder}"

# VoiceAgentSettings
set_single_value "$SITE_NAME" "VoiceAgentSettings" enabled                  "${VOICE_AGENT_ENABLED:-0}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" service_url              "${VOICE_AGENT_SERVICE_URL:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" client_id                "${VOICE_AGENT_CLIENT_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" client_secret            "${VOICE_AGENT_CLIENT_SECRET:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" default_contact_group_id "${VOICE_AGENT_DEFAULT_CONTACT_GROUP_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" agent_id                 "${VOICE_AGENT_AGENT_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" auth_token_cache_ttl     "${VOICE_AGENT_AUTH_TOKEN_CACHE_TTL:-3600}"

# Glific Settings → glific-stub
echo "Seeding Glific Settings → glific-stub..."
set_single_value "$SITE_NAME" "Glific Settings" api_url "${GLIFIC_API_URL:-http://glific-stub:4000}"
set_single_value "$SITE_NAME" "Glific Settings" api_key "${GLIFIC_API_KEY:-local-stub-key}"

bench --site "$SITE_NAME" migrate
bench --site "$SITE_NAME" clear-cache

# ── rag_service site settings ─────────────────────────────────────────────────
# rag_service needs its OWN RabbitMQ Settings (consumes submission_queue,
# publishes feedback_results_queue) and OWN GCS Settings (downloads
# submission media) — separate doctype records in its own DB, pointing at
# the same physical infra as the tap_lms copies above.

if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" host                     "${RABBITMQ_HOST:-}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" port                     "${RABBITMQ_PORT:-5672}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" virtual_host             "${RABBITMQ_VIRTUAL_HOST:-/}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" username                 "${RABBITMQ_USERNAME:-guest}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" password                 "${RABBITMQ_PASSWORD:-guest}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" submission_queue         "${RABBITMQ_SUBMISSION_QUEUE:-}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
  set_single_value "$RAG_SITE_NAME" "RabbitMQ Settings" feedback_results_queue   "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
fi

set_single_value "$RAG_SITE_NAME" "GCS Settings" project_id       "${GCS_PROJECT_ID:-}"
set_single_value "$RAG_SITE_NAME" "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

# RAG Settings → points back at the tap_lms site over HTTP
echo "Seeding RAG Settings..."
set_single_value "$RAG_SITE_NAME" "RAG Settings" base_url                    "http://${SITE_NAME}:${WEB_PORT:-8000}"
set_single_value "$RAG_SITE_NAME" "RAG Settings" assignment_context_endpoint "api/method/tap_lms.imgana.submission.get_assignment_context"
set_single_value "$RAG_SITE_NAME" "RAG Settings" student_context_endpoint    "api/method/tap_lms.imgana.submission.get_student_details"
set_single_value "$RAG_SITE_NAME" "RAG Settings" enable_caching              "0"

bench --site "$RAG_SITE_NAME" migrate
bench --site "$RAG_SITE_NAME" clear-cache
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
echo "Seeding LLM Settings & RAG Secrets (rag_service site)..."

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash << EOF
set -euo pipefail
cd /home/frappe/frappe-bench/sites
../env/bin/python3 - << 'PYEOF'
import frappe
import frappe.utils.password as frappe_crypt

frappe.init(site="$RAG_SITE_NAME")
frappe.connect()

# 1. LLM Settings (Stub) — lives on the rag_service site
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

# ── API Credentials for local development ────────────────────
API_KEY_VALUE = "$LOCAL_API_KEY"
API_SECRET_VALUE = "$LOCAL_API_SECRET"

# 2. Update RAG Settings with API key and vault the secret key securely.
#    Must match the Administrator API key/secret seeded on the tap_lms site
#    below — this is what rag_service sends as the Authorization header when
#    calling tap_lms's get_assignment_context / get_student_details.
rag_settings = frappe.get_doc("RAG Settings", "RAG Settings")
if rag_settings.api_key != API_KEY_VALUE:
    rag_settings.api_key = API_KEY_VALUE
    rag_settings.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ RAG Settings api_key set to: {API_KEY_VALUE}")
else:
    print(f"  RAG Settings api_key already set: {API_KEY_VALUE}")

frappe_crypt.set_encrypted_password(
    "RAG Settings", "RAG Settings", API_SECRET_VALUE, "api_secret"
)
frappe.db.commit()
print("✓ Seeded RAG Settings api_secret in secure vault")
PYEOF
EOF

echo "Seeding API Credentials (tap_lms site)..."

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash << EOF
set -euo pipefail
cd /home/frappe/frappe-bench/sites
../env/bin/python3 - << 'PYEOF'
import frappe
import frappe.utils.password as frappe_crypt

frappe.init(site="$SITE_NAME")
frappe.connect()

API_KEY_VALUE = "$LOCAL_API_KEY"
API_SECRET_VALUE = "$LOCAL_API_SECRET"

# 3. Update the User Profile directly with the public API key identifier.
#    This is the tap_lms-side credential that incoming requests (including
#    rag_service's calls above) authenticate against.
user_doc = frappe.get_doc("User", "Administrator")
if user_doc.api_key != API_KEY_VALUE:
    user_doc.api_key = API_KEY_VALUE
    user_doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"✓ Public API Key bound to User Profile: {API_KEY_VALUE}")
else:
    print(f"  Public API Key already set on User Profile: {API_KEY_VALUE}")

# 4. Force-inject the crypted Secret password block into Frappe's security vault for Administrator
current_secret = frappe_crypt.get_decrypted_password(
    "User", "Administrator", "api_secret", raise_exception=False
)

if current_secret != API_SECRET_VALUE:
    frappe_crypt.set_encrypted_password(
        "User", "Administrator", API_SECRET_VALUE, "api_secret"
    )
    frappe.db.commit()
    print(f"✓ API Secret encrypted and vaulted securely: {API_SECRET_VALUE}")
else:
    print(f"  API Secret already validated in vault.")
PYEOF
EOF

# ── Step 6: Create seed data ──────────────────────────────────────────────────────────
echo "Running seed_local.py (tap_lms site)..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash -lc '
  cd /home/frappe/frappe-bench/sites && \
  SITE_NAME="${SITE_NAME:-tap_lms.localhost}" \
  LOCAL_API_KEY="${LOCAL_API_KEY:-local-dev-api-key-001}" \
  LOCAL_API_SECRET="${LOCAL_API_SECRET:-local-secret-key}" \
  ../env/bin/python3 -c "import sys; sys.path.insert(0, \"/workspace/frappe_tap\"); import scripts.seed_local"
'

echo "Running seed_local_rag.py (rag_service site)..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev-lms bash -lc '
  cd /home/frappe/frappe-bench/sites && \
  SITE_NAME="${SITE_NAME:-tap_lms.localhost}" \
  RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}" \
  WEB_PORT="${WEB_PORT:-8000}" \
  LOCAL_API_KEY="${LOCAL_API_KEY:-local-dev-api-key-001}" \
  LOCAL_API_SECRET="${LOCAL_API_SECRET:-local-secret-key}" \
  ../env/bin/python3 -c "import sys; sys.path.insert(0, \"/workspace/frappe_tap\"); import scripts.seed_local_rag"
'

echo "Initializing Plagiarism Database..."
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T tap_plg_postgres psql -U postgres -d plagiarism_db < "$ROOT_DIR/../tap_plg/database/init.sql"
cat <<EOF

# Note: Infrastructure (postgres, rabbitmq, etc.) and the Plagiarism Service
# (tap_plg_worker/api) were started in Step 1.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Local TAP LMS testbed is ready.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Frappe LMS Service (tap_lms)    →  http://${SITE_NAME}:${WEB_PORT:-8000}
  Frappe RAG Service (rag)        →  http://${RAG_SITE_NAME}:${WEB_PORT:-8000}  (own DB: ${RAG_POSTGRES_DB})
  tap_plg API (Real ML Service)   →  http://localhost:${TAP_PLG_API_PORT:-8080}
  RabbitMQ management UI          →  http://localhost:15672  (guest / guest)
  LLM stub                        →  http://localhost:${LLM_STUB_PORT:-8001}
  Glific stub                     →  http://localhost:${GLIFIC_STUB_PORT:-4000}

  Admin user:     Administrator
  Admin password: ${ADMIN_PASSWORD}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Next steps:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Start the Frappe LMS web server:
   podman-compose --env-file ./frappe_tap/env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
     exec dev-lms bash -lc "cd /home/frappe/frappe-bench && bench start"

2. Start your RAG Worker Consumer (Feedback Generator) — note it now connects
   to the rag_service site (${RAG_SITE_NAME}), not tap_lms:
   podman-compose --env-file ./frappe_tap/env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
     exec dev-lms bash -lc "cd /home/frappe/frappe-bench/sites/ && SITE_NAME=${RAG_SITE_NAME} ../env/bin/python -c \"import rag_service.scripts.console_consumer as cc; cc.run()\""

2a. Start the LMS Submission consumer (Updates state):
    podman-compose --env-file ./frappe_tap/env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
    exec dev-lms bash -lc "cd /home/frappe/frappe-bench/sites/ && ../env/bin/python ../apps/tap_lms/scripts/console_consumer.py"

2b. Watch Plagiarism Worker logs (ML model loading takes 1-2 mins):
    podman-compose --env-file env.local -f ./frappe_tap/docker/local/docker-compose.local.yml \\
    logs -f tap_plg_worker

2c. Development (Hot Reloading):
    - Changes in 'tap_plg/api/' are auto-reloaded by uvicorn.
    - Changes in 'tap_plg/app.py' or 'tap_plg/plag_checker/' require a manual restart:
      podman restart tap_lms_plg_worker

3. Check if test API key is successfully created and associated with Administrator in Frappe bench. If not, create a test API key:
   Frappe desk → API Key → New → key: local-test-key-001 → Save

4. Send a test submission (ensure auth token matches one declared in seed script or one created above manually):
    curl -v -X POST "http://tap_lms.localhost:8000/api/method/tap_lms.imgana.submission.assignment_submission" -H "Content-Type: application/json" -H "Authorization: token ${LOCAL_API_KEY}:${LOCAL_API_SECRET}" -d '{
        "api_key":   "${LOCAL_API_KEY}",
        "assign_id": "MockAssign-Basic",
        "name1":     "LocalDevStudent",
        "glific_id": "LOCAL_GLIFIC_001",
        "submission": "https://picsum.photos/200/300"
    }'

5. Verify the pipeline:
   # 1. Check tap_plg API health
   curl http://localhost:${TAP_PLG_API_PORT:-8080}/health

   # 2. Check if plagiarism results were generated for the student
   # (Wait for the worker to finish processing)
   curl http://localhost:${TAP_PLG_API_PORT:-8080}/api/v1/results/LOCAL_GLIFIC_001 | python3 -m json.tool

   # 3. Glific stub received the WhatsApp trigger
   curl http://localhost:${GLIFIC_STUB_PORT:-4000}/stub/flow-calls | python3 -m json.tool

6. Reset stub state (LLM/Glific) between test runs:
   curl http://localhost:${GLIFIC_STUB_PORT:-4000}/stub/reset

7. Watch the full pipeline trace live:
   podman-compose --env-file env.local -f docker/local/docker-compose.local.yml logs -f

EOF
