#!/usr/bin/env bash
# Start a source-mounted AstraBox deployment for the browser E2E. This uses the
# maintained Compose networks and generated file secrets; it never exposes the
# Docker bridge gateway to a sandbox as a general callback host.
set -euo pipefail

REPO_ROOT="${ASTRABOX_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$REPO_ROOT"

ENV_FILE="${ASTRABOX_E2E_ENV_FILE:-$REPO_ROOT/.env}"
BACKEND_PORT="${ASTRABOX_BACKEND_PORT:-8123}"
COMPOSE_PROJECT="${ASTRABOX_E2E_COMPOSE_PROJECT:-astrabox-e2e-$BACKEND_PORT}"
SERVER_IMAGE="${ASTRABOX_SERVER_IMAGE:-astrabox/server:latest}"

fail() { printf 'serve-backend.sh: %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] \
  || fail "$ENV_FILE not found; the live E2E needs model provider settings"
command -v docker >/dev/null 2>&1 || fail "Docker CLI not found"
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
  || fail "Docker daemon is not reachable"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

SERVER_IMAGE="${ASTRABOX_SERVER_IMAGE:-$SERVER_IMAGE}"
docker image inspect "$SERVER_IMAGE" >/dev/null 2>&1 \
  || fail "server image $SERVER_IMAGE is missing; run 'make build-server-image'"

export ASTRABOX_SERVER_IMAGE="$SERVER_IMAGE"
export ASTRABOX_SERVER_HOST_PORT="$BACKEND_PORT"
export ASTRABOX_SERVER_SANDBOX_PORT="${ASTRABOX_E2E_SERVER_SANDBOX_PORT:-$((BACKEND_PORT + 10000))}"
export ASTRABOX_MODEL_GATEWAY_HOST_PORT="${ASTRABOX_E2E_MODEL_GATEWAY_HOST_PORT:-$((BACKEND_PORT + 11000))}"
export ASTRABOX_GATEWAY_DNS_HOST_PORT="${ASTRABOX_E2E_GATEWAY_DNS_HOST_PORT:-$((BACKEND_PORT + 12000))}"
export ASTRABOX_POSTGRES_PORT="${ASTRABOX_E2E_POSTGRES_PORT:-$((BACKEND_PORT + 30000))}"
export ASTRABOX_REDIS_PORT="${ASTRABOX_E2E_REDIS_PORT:-$((BACKEND_PORT + 31000))}"
export ASTRABOX_POSTGRES_VOLUME="${ASTRABOX_E2E_POSTGRES_VOLUME:-astrabox-e2e-$BACKEND_PORT-postgres}"
export ASTRABOX_STATE_VOLUME="${ASTRABOX_E2E_STATE_VOLUME:-astrabox-e2e-$BACKEND_PORT-state}"
export ASTRABOX_LOCAL_DATABASE_SECRET_DIR="${ASTRABOX_E2E_DATABASE_SECRET_DIR:-$REPO_ROOT/.e2e/database-secrets-$BACKEND_PORT}"
if [ -z "${DOCKER_GID:-}" ]; then
  DOCKER_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null \
    || stat -f %g /var/run/docker.sock 2>/dev/null)" \
    || fail "cannot determine the Docker socket group"
  export DOCKER_GID
fi

# Do not inherit another deployment's database or edge addresses from .env.
# E2E-scoped variables are the only supported topology overrides here.
export ASTRABOX_DB_URL="${ASTRABOX_E2E_DB_URL:-}"
export LITELLM_DATABASE_URL="${ASTRABOX_E2E_LITELLM_DATABASE_URL:-}"
export ASTRABOX_MCP_PROXY_BASE_URL="${ASTRABOX_E2E_MCP_PROXY_BASE_URL:-}"
export ASTRABOX_SANDBOX_GATEWAY_IP="${ASTRABOX_E2E_SANDBOX_EDGE_IP:-}"
export ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM="${ASTRABOX_E2E_SANDBOX_EGRESS_DNS_UPSTREAM:-}"
export ASTRABOX_LITELLM_BASE_URL="${ASTRABOX_E2E_LITELLM_BASE_URL:-}"
export ASTRABOX_LITELLM_SERVER_BASE_URL="${ASTRABOX_E2E_LITELLM_SERVER_BASE_URL:-}"

COMPOSE_ARGS=(
  --project-name "$COMPOSE_PROJECT"
  -f containers/compose.source.yaml
)

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  "$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" down -v --remove-orphans \
    >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

"$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" up -d --wait --no-build
printf '%s\n' \
  "[serve-backend] backend=http://127.0.0.1:$BACKEND_PORT" \
  "[serve-backend] database=private generated_secrets=yes" \
  "[serve-backend] sandbox_callback=dedicated-edge"

# Playwright owns this process. Following the server log keeps it alive; a TERM
# from Playwright runs the trap above and removes only this project and its test
# volumes.
"$REPO_ROOT/scripts/compose.sh" "${COMPOSE_ARGS[@]}" logs -f --no-log-prefix server
