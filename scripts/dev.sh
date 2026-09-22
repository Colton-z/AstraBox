#!/usr/bin/env bash
# One-command source development stack. Python code is mounted into the
# maintained container topology, so local development exercises the same
# private database network and sandbox-edge boundary as deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${ASTRABOX_DEV_ENV_FILE:-$REPO_ROOT/.env}"
BACKEND_PORT="${ASTRABOX_DEV_BACKEND_PORT:-8000}"
FRONTEND_PORT="${ASTRABOX_DEV_FRONTEND_PORT:-5173}"
COMPOSE_PROJECT="${ASTRABOX_DEV_COMPOSE_PROJECT:-astrabox-dev}"
SERVER_IMAGE="${ASTRABOX_SERVER_IMAGE:-astrabox/server:latest}"

log() { printf '\033[36m[dev]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[dev] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker CLI not found"
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
  || die "Docker daemon is not reachable"

if [ -f "$ENV_FILE" ]; then
  log "loading provider settings from $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  log "no $ENV_FILE; the console will open, but a real turn needs a model provider key"
fi

SERVER_IMAGE="${ASTRABOX_SERVER_IMAGE:-$SERVER_IMAGE}"
docker image inspect "$SERVER_IMAGE" >/dev/null 2>&1 \
  || die "server image $SERVER_IMAGE is missing; run 'make build-server-image' once"

# Use task-owned names and ports so this stack cannot attach to or stop another
# Compose deployment on the same workstation.
export ASTRABOX_SERVER_IMAGE="$SERVER_IMAGE"
export ASTRABOX_SERVER_HOST_PORT="$BACKEND_PORT"
export ASTRABOX_SERVER_SANDBOX_PORT="${ASTRABOX_DEV_SERVER_SANDBOX_PORT:-$((BACKEND_PORT + 10000))}"
export ASTRABOX_MODEL_GATEWAY_HOST_PORT="${ASTRABOX_DEV_MODEL_GATEWAY_HOST_PORT:-$((BACKEND_PORT + 11000))}"
export ASTRABOX_GATEWAY_DNS_HOST_PORT="${ASTRABOX_DEV_GATEWAY_DNS_HOST_PORT:-$((BACKEND_PORT + 12000))}"
export ASTRABOX_POSTGRES_PORT="${ASTRABOX_DEV_POSTGRES_PORT:-$((BACKEND_PORT + 30000))}"
export ASTRABOX_REDIS_PORT="${ASTRABOX_DEV_REDIS_PORT:-$((BACKEND_PORT + 31000))}"
export ASTRABOX_POSTGRES_VOLUME="${ASTRABOX_DEV_POSTGRES_VOLUME:-astrabox-dev-postgres}"
export ASTRABOX_STATE_VOLUME="${ASTRABOX_DEV_STATE_VOLUME:-astrabox-dev-state}"
export ASTRABOX_LOCAL_DATABASE_SECRET_DIR="${ASTRABOX_DEV_DATABASE_SECRET_DIR:-$REPO_ROOT/.astrabox/dev-database-secrets}"
if [ -z "${DOCKER_GID:-}" ]; then
  DOCKER_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null \
    || stat -f %g /var/run/docker.sock 2>/dev/null)" \
    || die "cannot determine the Docker socket group"
  export DOCKER_GID
fi

# Deployment-wide values from an unrelated .env must not bypass this stack's
# generated database files or dynamic edge discovery. Dedicated dev overrides
# remain available for deliberately external services.
export ASTRABOX_DB_URL="${ASTRABOX_DEV_DB_URL:-}"
export LITELLM_DATABASE_URL="${ASTRABOX_DEV_LITELLM_DATABASE_URL:-}"
export ASTRABOX_MCP_PROXY_BASE_URL="${ASTRABOX_DEV_MCP_PROXY_BASE_URL:-}"
export ASTRABOX_SANDBOX_GATEWAY_IP="${ASTRABOX_DEV_SANDBOX_EDGE_IP:-}"
export ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM="${ASTRABOX_DEV_SANDBOX_EGRESS_DNS_UPSTREAM:-}"
export ASTRABOX_LITELLM_BASE_URL="${ASTRABOX_DEV_LITELLM_BASE_URL:-}"
export ASTRABOX_LITELLM_SERVER_BASE_URL="${ASTRABOX_DEV_LITELLM_SERVER_BASE_URL:-}"
export ASTRABOX_DEV_PROXY_TARGET="http://127.0.0.1:$BACKEND_PORT"

COMPOSE_ARGS=(
  --project-name "$COMPOSE_PROJECT"
  -f containers/compose.source.yaml
)
PIDS=()

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true
  done
  log "stopping source-development containers"
  "$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" down --remove-orphans \
    >/dev/null 2>&1 || true
  wait 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

"$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" up -d --wait --no-build
log "backend  -> http://127.0.0.1:$BACKEND_PORT  (containerized uvicorn --reload)"
log "database -> private Compose network; generated credentials in $ASTRABOX_LOCAL_DATABASE_SECRET_DIR"

"$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" logs -f --no-log-prefix server &
PIDS+=("$!")

if [ "${ASTRABOX_DEV_BACKEND_ONLY:-0}" != "1" ]; then
  if [ -d "$REPO_ROOT/frontend/node_modules" ]; then
    log "frontend -> http://127.0.0.1:$FRONTEND_PORT"
    (
      cd "$REPO_ROOT/frontend"
      exec python3 "$REPO_ROOT/scripts/node-toolchain.py" \
        npm run dev -- --port "$FRONTEND_PORT" --strictPort
    ) &
    PIDS+=("$!")
  else
    log "frontend/node_modules is missing; run 'make install-web'"
  fi
fi

log "stack is ready; Ctrl-C stops its containers but preserves its data volumes"
wait -n
