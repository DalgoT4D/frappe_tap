#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/local/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/env.local}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env.local. Copy .env.example to env.local and fill in the required values."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build postgres redis-cache redis-queue rabbitmq dev
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev chown -R frappe:frappe /home/frappe/frappe-bench
podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev chmod -R a+rX /workspace/frappe_tap

podman-compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev bash -lc '
set -euo pipefail

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

if [[ ! -d /home/frappe/frappe-bench/apps/frappe ]]; then
  if [[ -n "$(find /home/frappe/frappe-bench -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "/home/frappe/frappe-bench is not empty but Frappe is missing."
    echo "If this is a broken local setup, reset it with: podman-compose --env-file .env -f docker/local/docker-compose.yml down -v"
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

if [[ ! -L apps/tap_lms ]]; then
  echo "apps/tap_lms exists but is not a symlink to /workspace/frappe_tap."
  echo "Move or remove it before rerunning setup if you want live local code mounted."
  exit 1
fi

./env/bin/python -m pip install -q -e /workspace/frappe_tap
bench build --app tap_lms

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
    --admin-password "$ADMIN_PASSWORD" \
    --install-app tap_lms
else
  bench --site "$SITE_NAME" migrate
fi

if ! bench --site "$SITE_NAME" list-apps | grep -qx "business_theme_v14"; then
  bench --site "$SITE_NAME" install-app business_theme_v14
fi

bench --site "$SITE_NAME" set-config developer_mode 1
bench --site "$SITE_NAME" set-config host_name "http://tap_lms.localhost:${WEB_PORT:-8000}"

set_single_value() {
  local doctype="$1"
  local field="$2"
  local value="${3:-}"
  local args
  args="$(python -c "import json,sys; print(json.dumps([sys.argv[1], sys.argv[2], sys.argv[3]]))" "$doctype" "$field" "$value")"
  bench --site "$SITE_NAME" execute frappe.db.set_single_value --args "$args"
}

if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  set_single_value "RabbitMQ Settings" host "${RABBITMQ_HOST:-}"
  set_single_value "RabbitMQ Settings" port "${RABBITMQ_PORT:-5671}"
  set_single_value "RabbitMQ Settings" virtual_host "${RABBITMQ_VIRTUAL_HOST:-}"
  set_single_value "RabbitMQ Settings" username "${RABBITMQ_USERNAME:-}"
  set_single_value "RabbitMQ Settings" password "${RABBITMQ_PASSWORD:-}"
  set_single_value "RabbitMQ Settings" submission_queue "${RABBITMQ_SUBMISSION_QUEUE:-}"
  set_single_value "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
  set_single_value "RabbitMQ Settings" feedback_results_queue "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
fi

set_single_value "GCS Settings" enabled "${GCS_ENABLED:-0}"
set_single_value "GCS Settings" bucket_name "${GCS_BUCKET_NAME:-}"
set_single_value "GCS Settings" project_id "${GCS_PROJECT_ID:-}"
set_single_value "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

set_single_value "ElevenLabs Settings" enabled "${ELEVENLABS_ENABLED:-0}"
set_single_value "ElevenLabs Settings" api_key "${ELEVENLABS_API_KEY:-disabled-local-placeholder}"

set_single_value "VoiceAgentSettings" enabled "${VOICE_AGENT_ENABLED:-0}"
set_single_value "VoiceAgentSettings" service_url "${VOICE_AGENT_SERVICE_URL:-}"
set_single_value "VoiceAgentSettings" client_id "${VOICE_AGENT_CLIENT_ID:-}"
set_single_value "VoiceAgentSettings" client_secret "${VOICE_AGENT_CLIENT_SECRET:-}"
set_single_value "VoiceAgentSettings" default_contact_group_id "${VOICE_AGENT_DEFAULT_CONTACT_GROUP_ID:-}"
set_single_value "VoiceAgentSettings" agent_id "${VOICE_AGENT_AGENT_ID:-}"
set_single_value "VoiceAgentSettings" auth_token_cache_ttl "${VOICE_AGENT_AUTH_TOKEN_CACHE_TTL:-3600}"

bench --site "$SITE_NAME" clear-cache
'

cat <<EOF

Local tap_lms setup is ready.

URL: http://tap_lms.localhost:${WEB_PORT:-8000}
Admin user: Administrator
Admin password: ${ADMIN_PASSWORD}

Start the web server:
  podman-compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "cd /home/frappe/frappe-bench && bench start"

EOF
