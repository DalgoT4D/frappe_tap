#!/usr/bin/env bash
# Runs INSIDE the dev-lms container (PID-1 entrypoint calls this automatically;
# you can also run it by hand for debugging):
#
#   podman-compose -f docker/local/docker-compose.local.yml exec dev-lms \
#     bash /workspace/frappe_tap/scripts/bootstrap_lms.sh
#
# All env vars (SITE_NAME, RAG_SITE_NAME, POSTGRES_USER, ...) already exist in
# the container because docker-compose.local.yml loads env.local via
# `env_file:` — no more host-side interpolation tricks needed.
#
# Safe to re-run: every expensive/idempotent step is guarded.
set -euo pipefail

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

RAG_SITE_NAME="${RAG_SITE_NAME:-rag.localhost}"
RAG_POSTGRES_DB="${RAG_POSTGRES_DB:-rag_lms}"

LOCAL_API_KEY="${LOCAL_API_KEY:-local-dev-api-key-001}"
LOCAL_API_SECRET="${LOCAL_API_SECRET:-local-secret-key}"

set_single_value() {
  local site="$1" doctype="$2" field="$3" value="${4:-}"
  local args
  args="$(python -c "import json,sys; print(json.dumps([sys.argv[1], sys.argv[2], sys.argv[3]]))" "$doctype" "$field" "$value")"
  bench --site "$site" execute frappe.db.set_single_value --args "$args"
}

# ── Step A: bench init (only if missing) ──────────────────────────────────
if [[ ! -d /home/frappe/frappe-bench/apps/frappe ]]; then
  if [[ -n "$(find /home/frappe/frappe-bench -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "/home/frappe/frappe-bench is not empty but Frappe is missing."
    echo "Reset it with: podman-compose -f docker/local/docker-compose.local.yml down -v"
    exit 1
  fi
  cd /home/frappe
  bench init --frappe-branch "$FRAPPE_BRANCH" --skip-redis-config-generation --ignore-exist frappe-bench
fi

cd /home/frappe/frappe-bench

bench set-config -g db_host postgres
bench set-config -g db_port 5432
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -g socketio_port 9000

[[ -e apps/tap_lms ]]    || ln -s /workspace/frappe_tap apps/tap_lms
[[ -e apps/rag_service ]] || ln -s /workspace/rag_service apps/rag_service

./env/bin/python -m pip install -q --upgrade pip setuptools wheel flit_core
./env/bin/python -m pip install -q -e /workspace/frappe_tap --no-build-isolation
./env/bin/python -m pip install -q -e /workspace/rag_service --no-deps --no-build-isolation

[[ -d apps/business_theme_v14 ]] || bench get-app "$BUSINESS_THEME_REPO"

printf 'frappe\ntap_lms\nbusiness_theme_v14\nrag_service\n' > apps.txt
printf 'frappe\ntap_lms\nbusiness_theme_v14\nrag_service\n' > sites/apps.txt

# ── Step B: tap_lms site ───────────────────────────────────────────────────
if [[ ! -d "sites/$SITE_NAME" ]]; then
  bench new-site "$SITE_NAME" \
    --db-type postgres --db-host postgres --db-port 5432 \
    --db-root-username "$POSTGRES_USER" --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
fi

bench --site "$SITE_NAME" install-app tap_lms
bench --site "$SITE_NAME" install-app business_theme_v14
bench --site "$SITE_NAME" migrate

bench build --app tap_lms
bench build --app business_theme_v14

bench --site "$SITE_NAME" set-config developer_mode 1
bench --site "$SITE_NAME" set-config host_name "http://${SITE_NAME}:${WEB_PORT:-8000}"

# ── Step C: rag_service site (separate DB) ─────────────────────────────────
if [[ ! -d "sites/$RAG_SITE_NAME" ]]; then
  bench new-site "$RAG_SITE_NAME" \
    --db-type postgres --db-host postgres --db-port 5432 --db-name "$RAG_POSTGRES_DB" \
    --db-root-username "$POSTGRES_USER" --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD"
fi

bench --site "$RAG_SITE_NAME" install-app rag_service
bench --site "$RAG_SITE_NAME" migrate
bench --site "$RAG_SITE_NAME" set-config developer_mode 1

# ── Step D: settings (cheap — fine to always re-apply) ─────────────────────
if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  for site in "$SITE_NAME" "$RAG_SITE_NAME"; do
    set_single_value "$site" "RabbitMQ Settings" host                     "${RABBITMQ_HOST:-}"
    set_single_value "$site" "RabbitMQ Settings" port                     "${RABBITMQ_PORT:-5672}"
    set_single_value "$site" "RabbitMQ Settings" virtual_host             "${RABBITMQ_VIRTUAL_HOST:-/}"
    set_single_value "$site" "RabbitMQ Settings" username                 "${RABBITMQ_USERNAME:-guest}"
    set_single_value "$site" "RabbitMQ Settings" password                 "${RABBITMQ_PASSWORD:-guest}"
    set_single_value "$site" "RabbitMQ Settings" submission_queue         "${RABBITMQ_SUBMISSION_QUEUE:-}"
    set_single_value "$site" "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
    set_single_value "$site" "RabbitMQ Settings" feedback_results_queue   "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
  done
fi

set_single_value "$SITE_NAME" "GCS Settings" enabled          "${GCS_ENABLED:-0}"
set_single_value "$SITE_NAME" "GCS Settings" bucket_name      "${GCS_BUCKET_NAME:-}"
set_single_value "$SITE_NAME" "GCS Settings" project_id       "${GCS_PROJECT_ID:-}"
set_single_value "$SITE_NAME" "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

set_single_value "$SITE_NAME" "ElevenLabs Settings" enabled "${ELEVENLABS_ENABLED:-0}"
set_single_value "$SITE_NAME" "ElevenLabs Settings" api_key  "${ELEVENLABS_API_KEY:-disabled-local-placeholder}"

set_single_value "$SITE_NAME" "VoiceAgentSettings" enabled                  "${VOICE_AGENT_ENABLED:-0}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" service_url              "${VOICE_AGENT_SERVICE_URL:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" client_id                "${VOICE_AGENT_CLIENT_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" client_secret            "${VOICE_AGENT_CLIENT_SECRET:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" default_contact_group_id "${VOICE_AGENT_DEFAULT_CONTACT_GROUP_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" agent_id                 "${VOICE_AGENT_AGENT_ID:-}"
set_single_value "$SITE_NAME" "VoiceAgentSettings" auth_token_cache_ttl     "${VOICE_AGENT_AUTH_TOKEN_CACHE_TTL:-3600}"

echo "Seeding Glific Settings -> glific-stub..."
set_single_value "$SITE_NAME" "Glific Settings" api_url "${GLIFIC_API_URL:-http://glific-stub:4000}"
set_single_value "$SITE_NAME" "Glific Settings" api_key "${GLIFIC_API_KEY:-local-stub-key}"

bench --site "$SITE_NAME" migrate
bench --site "$SITE_NAME" clear-cache

set_single_value "$RAG_SITE_NAME" "GCS Settings" project_id       "${GCS_PROJECT_ID:-}"
set_single_value "$RAG_SITE_NAME" "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

echo "Seeding RAG Settings..."
set_single_value "$RAG_SITE_NAME" "RAG Settings" base_url                    "http://${SITE_NAME}:${WEB_PORT:-8000}"
set_single_value "$RAG_SITE_NAME" "RAG Settings" assignment_context_endpoint "api/method/tap_lms.imgana.submission.get_assignment_context"
set_single_value "$RAG_SITE_NAME" "RAG Settings" student_context_endpoint    "api/method/tap_lms.imgana.submission.get_student_details"
set_single_value "$RAG_SITE_NAME" "RAG Settings" enable_caching              "0"

bench --site "$RAG_SITE_NAME" migrate
bench --site "$RAG_SITE_NAME" clear-cache

# ── Step E: rag_service isolated venv (THE SLOW STEP — now skipped on repeat runs) ──
echo "Setting up rag_service isolated venv..."

RAG_VENV="/home/frappe/rag_venv"
RAG_REQ_FILE="/workspace/rag_service/requirements.txt"
RAG_HASH_FILE="$RAG_VENV/.requirements.sha256"
CURRENT_HASH="$(sha256sum "$RAG_REQ_FILE" | awk '{print $1}')"

if [[ -x "$RAG_VENV/bin/python3" ]] && [[ -f "$RAG_HASH_FILE" ]] && [[ "$(cat "$RAG_HASH_FILE")" == "$CURRENT_HASH" ]]; then
  echo "  rag_service venv already up to date (requirements.txt unchanged) — skipping install."
else
  echo "  Installing rag_service dependencies (venv missing or requirements.txt changed)..."

  # Build securely in a temporary directory, swap only on 100% success
  TMP_VENV="/home/frappe/rag_venv_tmp"
  rm -rf "$TMP_VENV"
  python3 -m venv "$TMP_VENV"
  "$TMP_VENV/bin/pip" install -r "$RAG_REQ_FILE"
  echo "$CURRENT_HASH" > "$TMP_VENV/.requirements.sha256"

  # Clean the CONTENTS of the directory instead of trying to delete the mounted folder, which causes the container to crash
  find "$RAG_VENV" -mindepth 1 -delete 2>/dev/null || true

  # copy tmp contents to the RAG env
  cp -a "$TMP_VENV/." "$RAG_VENV/"
  rm -rf "$TMP_VENV"
  echo "----- Finished installing rag_service dependencies... ----"
fi

# Bridge the rag venv into Frappe's venv via a .pth file (cheap, always safe to redo)
./env/bin/python3 - <<'PYEOF'
import site, os, sys
pth_dir = site.getsitepackages()[0]
py_ver = "python{}.{}".format(sys.version_info.major, sys.version_info.minor)
rag_site = "/home/frappe/rag_venv/lib/{}/site-packages".format(py_ver)
pth_file = os.path.join(pth_dir, "rag_isolated.pth")
open(pth_file, "w").write(rag_site + "\n")
print("Bridged: {} -> {}".format(pth_file, rag_site))
PYEOF

./env/bin/python3 -c "from rag_service.utils.rabbitmq_consumer import RabbitMQConsumer; print('rag_service import OK')"
echo "rag_service venv bridge complete."

# ── Step F: LLM Settings + API credentials ──────────────────────────────────
echo "Seeding LLM Settings & RAG Secrets (rag_service site)..."
cd /home/frappe/frappe-bench/sites
../env/bin/python3 - <<PYEOF
import frappe
import frappe.utils.password as frappe_crypt

frappe.init(site="$RAG_SITE_NAME")
frappe.connect()

if not frappe.db.exists("LLM Settings", {"provider": "Stub"}):
    doc = frappe.new_doc("LLM Settings")
    doc.provider = "Stub"
    doc.model_name = "${LLM_MODEL_NAME:-stub-gpt-4}"
    doc.base_url = "${LLM_BASE_URL:-http://llm-stub:8001}"
    doc.api_key = "${LLM_API_KEY:-local-stub-key}"
    doc.is_active = 1
    doc.insert()
    print("Created Stub LLM Settings")
else:
    doc = frappe.get_doc("LLM Settings", {"provider": "Stub"})
    doc.model_name = "${LLM_MODEL_NAME:-stub-gpt-4}"
    doc.base_url = "${LLM_BASE_URL:-http://llm-stub:8001}"
    doc.is_active = 1
    doc.save()
    print("Updated Stub LLM Settings")

API_KEY_VALUE = "$LOCAL_API_KEY"
API_SECRET_VALUE = "$LOCAL_API_SECRET"

rag_settings = frappe.get_doc("RAG Settings", "RAG Settings")
if rag_settings.api_key != API_KEY_VALUE:
    rag_settings.api_key = API_KEY_VALUE
    rag_settings.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"RAG Settings api_key set to: {API_KEY_VALUE}")

frappe_crypt.set_encrypted_password("RAG Settings", "RAG Settings", API_SECRET_VALUE, "api_secret")
frappe.db.commit()
print("Seeded RAG Settings api_secret in secure vault")
PYEOF

echo "Seeding API Credentials (tap_lms site)..."
../env/bin/python3 - <<PYEOF
import frappe
import frappe.utils.password as frappe_crypt

frappe.init(site="$SITE_NAME")
frappe.connect()

API_KEY_VALUE = "$LOCAL_API_KEY"
API_SECRET_VALUE = "$LOCAL_API_SECRET"

user_doc = frappe.get_doc("User", "Administrator")
if user_doc.api_key != API_KEY_VALUE:
    user_doc.api_key = API_KEY_VALUE
    user_doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"Public API Key bound to User Profile: {API_KEY_VALUE}")

current_secret = frappe_crypt.get_decrypted_password("User", "Administrator", "api_secret", raise_exception=False)
if current_secret != API_SECRET_VALUE:
    frappe_crypt.set_encrypted_password("User", "Administrator", API_SECRET_VALUE, "api_secret")
    frappe.db.commit()
    print(f"API Secret encrypted and vaulted securely: {API_SECRET_VALUE}")
PYEOF

# ── Step G: seed data (scripts are self-guarded / idempotent) ──────────────
echo "Running seed_local.py (tap_lms site)..."
SITE_NAME="$SITE_NAME" LOCAL_API_KEY="$LOCAL_API_KEY" LOCAL_API_SECRET="$LOCAL_API_SECRET" \
  ../env/bin/python3 -c "import sys; sys.path.insert(0, '/workspace/frappe_tap'); import scripts.seed_local"

echo "Running seed_local_rag.py (rag_service site)..."
SITE_NAME="$SITE_NAME" RAG_SITE_NAME="$RAG_SITE_NAME" WEB_PORT="${WEB_PORT:-8000}" \
  LOCAL_API_KEY="$LOCAL_API_KEY" LOCAL_API_SECRET="$LOCAL_API_SECRET" \
  ../env/bin/python3 -c "import sys; sys.path.insert(0, '/workspace/frappe_tap'); import scripts.seed_local_rag"

echo "Bootstrap complete."
