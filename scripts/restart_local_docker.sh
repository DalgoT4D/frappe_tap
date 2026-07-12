#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/local/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/env.local}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env.local. Copy .env.example to env.local and fill in the required values."
  exit 1
fi

BUILD_ASSETS=0

if [[ "${1:-}" == "--build" ]]; then
  BUILD_ASSETS=1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev bash -lc "
set -euo pipefail
cd /home/frappe/frappe-bench

if [[ $BUILD_ASSETS -eq 1 ]]; then
  bench build --app tap_lms
fi

bench restart
"
