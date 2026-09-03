#!/usr/bin/env bash
set -euo pipefail

APP_CONTAINER="${APP_CONTAINER:-tap_lms_dev}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-tap_lms_postgres}"
REDIS_CACHE_CONTAINER="${REDIS_CACHE_CONTAINER:-tap_lms_redis_cache}"
REDIS_QUEUE_CONTAINER="${REDIS_QUEUE_CONTAINER:-tap_lms_redis_queue}"
BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"
SITE="${SITE:-tap_lms.localhost}"
WEB_URL="${WEB_URL:-http://127.0.0.1:8000}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fx "$1" >/dev/null
}

container_running() {
  docker ps --format '{{.Names}}' | grep -Fx "$1" >/dev/null
}

container_health() {
  local status

  status="$(docker ps --filter "name=^/${1}$" --format '{{.Status}}')"

  case "$status" in
    *"(healthy)"*) printf 'healthy\n' ;;
    *"(unhealthy)"*) printf 'unhealthy\n' ;;
    *"(health:"*) printf 'starting\n' ;;
    *) printf 'none\n' ;;
  esac
}

start_container_if_needed() {
  local container="$1"

  container_exists "$container" || die "Docker container '$container' does not exist."

  if container_running "$container"; then
    log "$container is already running."
    return
  fi

  log "Starting $container..."
  docker start "$container" >/dev/null
}

wait_for_running() {
  local container="$1"
  local deadline=$((SECONDS + WAIT_SECONDS))

  until container_running "$container"; do
    [ "$SECONDS" -lt "$deadline" ] || die "$container did not start within ${WAIT_SECONDS}s."
    sleep 2
  done
}

wait_for_healthy_if_configured() {
  local container="$1"
  local deadline=$((SECONDS + WAIT_SECONDS))
  local health

  health="$(container_health "$container")"
  if [ "$health" = "none" ]; then
    return
  fi

  until [ "$health" = "healthy" ]; do
    [ "$SECONDS" -lt "$deadline" ] || die "$container health is '$health' after ${WAIT_SECONDS}s."
    sleep 2
    health="$(container_health "$container")"
  done

  log "$container is healthy."
}

bench_is_running() {
  docker exec "$APP_CONTAINER" pgrep -f 'honcho start|bench_helper frappe serve --port 8000' >/dev/null 2>&1
}

wait_for_http() {
  local deadline=$((SECONDS + WAIT_SECONDS))

  until docker exec "$APP_CONTAINER" curl -fsSI --max-time 3 "$WEB_URL" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "Frappe did not respond at $WEB_URL within ${WAIT_SECONDS}s."
    sleep 2
  done
}

command -v docker >/dev/null 2>&1 || die "docker command is not available."

start_container_if_needed "$POSTGRES_CONTAINER"
start_container_if_needed "$REDIS_CACHE_CONTAINER"
start_container_if_needed "$REDIS_QUEUE_CONTAINER"
start_container_if_needed "$APP_CONTAINER"

wait_for_running "$POSTGRES_CONTAINER"
wait_for_running "$REDIS_CACHE_CONTAINER"
wait_for_running "$REDIS_QUEUE_CONTAINER"
wait_for_running "$APP_CONTAINER"
wait_for_healthy_if_configured "$POSTGRES_CONTAINER"

if bench_is_running; then
  log "Frappe bench is already running in $APP_CONTAINER."
else
  log "Starting Frappe bench in $APP_CONTAINER..."
  docker exec -d -w "$BENCH_DIR" "$APP_CONTAINER" bench start
fi

wait_for_http
docker exec -w "$BENCH_DIR" "$APP_CONTAINER" bench --site "$SITE" doctor

log "Frappe is running at http://localhost:8000."
