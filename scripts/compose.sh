#!/usr/bin/env bash
# Maintained local Compose entry point. It creates persistent random database
# credentials before Compose resolves its service-scoped secret files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECRET_DIR="${ASTRABOX_LOCAL_DATABASE_SECRET_DIR:-$REPO_ROOT/.astrabox/database-secrets}"
POSTGRES_VOLUME="${ASTRABOX_POSTGRES_VOLUME:-astrabox-postgres}"
PYTHON_BIN="${ASTRABOX_COMPOSE_PYTHON:-python3}"

missing_secret=0
for secret_name in \
  postgres_admin_password \
  astrabox_password \
  litellm_password \
  casdoor_password
do
  if [ ! -f "$SECRET_DIR/$secret_name" ]; then
    missing_secret=1
  fi
done

if [ "$missing_secret" = 1 ] && docker volume inspect "$POSTGRES_VOLUME" >/dev/null 2>&1; then
  printf '%s\n' \
    "database credentials are missing but PostgreSQL volume '$POSTGRES_VOLUME' still exists." \
    "Restore $SECRET_DIR, or follow docs/deploy.md to rotate the database roles before starting the stack." >&2
  exit 1
fi

"$PYTHON_BIN" "$REPO_ROOT/scripts/ensure_local_database_secrets.py" \
  --directory "$SECRET_DIR" >/dev/null

export ASTRABOX_LOCAL_DATABASE_SECRET_DIR="$SECRET_DIR"
# A checkout runs the images built from it: `make build-*` and
# `up --build` name them astrabox/<component>:latest. The server reads the
# same two values to name every engine's default sandbox image. An installed
# release (scripts/install.sh) sets its published prefix and version instead.
export ASTRABOX_IMAGE_PREFIX="${ASTRABOX_IMAGE_PREFIX:-astrabox/}"
export ASTRABOX_IMAGE_TAG="${ASTRABOX_IMAGE_TAG:-latest}"
if [ -S /var/run/docker.sock ] && [ -z "${DOCKER_GID:-}" ]; then
  export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
fi

cd "$REPO_ROOT"
# Name the project. Compose otherwise names it after the compose file's parent
# directory, which is `containers/` — so a deployment came up as
# `containers-server-1`, `containers-postgres-1`, and on a host running
# anything else those names said nothing about what they were. The volumes
# carry pinned names of their own (containers/compose.yaml), so this is a name
# and not a migration: data does not move when the project is renamed.
exec docker compose \
  -p "${COMPOSE_PROJECT_NAME:-astrabox}" \
  -f "$REPO_ROOT/containers/compose.yaml" "$@"
