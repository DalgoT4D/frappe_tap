# Use this script to reset values to point to local env
# after restoring the DEV DB
# Usage:

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

bench --site "$SITE_NAME" migrate
bench --site "$SITE_NAME" clear-cache

# ── Step 4: Create separate venv & bridge for rag_service due to dependency conflicts ────────────────
echo "Setting up rag_service isolated venv..."

# single-quoted delimiter around 'OUTEREOF' heredoc below means:
# No variable expansion inside the heredoc (the $ signs are safe)
# No quote conflicts with the surrounding bash single-quote block
# The Python code itself can use any quotes freely

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

echo "rag_service venv bridge complete."

# ── Step 5: Seed LLM Settings & RAG Secrets ───────────────────────────────────
echo "Seeding LLM Settings & RAG Secrets..."

cd /home/frappe/frappe-bench/sites
../env/bin/python3 - << PYEOF
import frappe
import frappe.utils.password as frappe_crypt

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

# ── API Credentials for local development ────────────────────
API_KEY_VALUE = "local-dev-api-key-001"
API_SECRET_VALUE = "local-secret-key"

# 2. Update RAG Settings with API key and vault the secret key securely
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

# 3. Update the User Profile directly with the public API key identifier
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

