#!/usr/bin/env bash
# Install or upgrade AstraBox on one Docker host.
#
#   curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh | bash
#
# The installer downloads the deployment bundle of one exact release, generates
# the deployment's secrets, asks for a model service, starts the stack with
# Docker Compose, and waits until the console is served. Nothing is cloned or
# built: the stack runs the images published for that release,
# ghcr.io/colton-z/astrabox-<component>:<version>.
#
# Running it again upgrades in place. It installs the requested release's
# Compose files over the old ones and keeps the database and state volumes, the
# generated secrets, the model settings and every other line of
# <install dir>/containers/.env.
#
# Settings (all optional):
#   ASTRABOX_VERSION                 release to install, such as 0.1.0; default: latest release
#   ASTRABOX_INSTALL_DIR             installation directory; default: ~/astrabox
#   ASTRABOX_INSTALL_BUNDLE          path of a downloaded astrabox-deploy-<version>.tar.gz,
#                                    for a host that cannot reach GitHub
#   ASTRABOX_IMAGE_PREFIX            registry prefix of a mirror holding the published
#                                    images; default: ghcr.io/colton-z/astrabox-
#   ASTRABOX_INSTALL_MODEL_PROVIDER  anthropic, deepseek, anthropic-compatible,
#                                    openai-compatible or none. Setting it answers
#                                    every model question from the variables below
#                                    instead of asking.
#   ASTRABOX_INSTALL_MODEL_API_KEY   the model service's API key
#   ASTRABOX_INSTALL_MODEL_NAME      the model ID the seeded Agents use
#   ASTRABOX_INSTALL_MODEL_BASE_URL  base URL of an *-compatible service
#
# Docs: https://www.astrabox.ai/docs/deploy
set -euo pipefail

readonly REPOSITORY="Colton-z/AstraBox"
readonly DOCS_URL="https://www.astrabox.ai/docs/deploy"
readonly MODELS_DOCS_URL="https://www.astrabox.ai/docs/models"
readonly TEAM_LOGIN_DOCS_URL="https://www.astrabox.ai/docs/team-login"

# The same seven files, in the same format and modes, that
# scripts/ensure_local_database_secrets.py creates for a checkout. The Compose
# file reads the database ones as service secrets; the others belong to the
# team-login overlays.
readonly SECRET_NAMES=(
  postgres_admin_password
  astrabox_password
  litellm_password
  casdoor_password
  oidc_client_secret
  oidc_api_client_secret
  casdoor_admin_password
)
readonly DATABASE_SECRET_NAMES=(
  postgres_admin_password
  astrabox_password
  litellm_password
  casdoor_password
)

# DeepSeek is an Anthropic-compatible service with a known endpoint; the
# deployment derives the rest (astrabox/deploy/onebox.py), including the key for
# the OpenAI-wire routes other Agent programs use. `GET
# https://api.deepseek.com/v1/models` lists the model IDs it serves.
readonly DEEPSEEK_BASE_URL="https://api.deepseek.com/anthropic"
readonly DEEPSEEK_DEFAULT_MODEL="deepseek-flash"

# The bundled gateway route an OpenAI-compatible service is reached through
# (containers/litellm/config.yaml): an Agent selects `<prefix><model id>`.
readonly OPENAI_COMPATIBLE_ROUTE_PREFIX="openai-compatible/"

# Every setting the installer writes for a model service. Choosing a service
# removes all of them first, so the previous service's settings do not linger.
readonly MODEL_KEYS=(
  ASTRABOX_INSTALL_MODEL_PROVIDER
  ANTHROPIC_API_KEY
  ANTHROPIC_BASE_URL
  ANTHROPIC_MODEL
  OPENAI_COMPATIBLE_API_KEY
  OPENAI_COMPATIBLE_BASE_URL
)

readonly READY_TIMEOUT_SECONDS=600

# Progress goes to stderr: several steps run inside command substitutions.
step() { printf '==> %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$1" >&2
  shift
  local line
  for line in "$@"; do printf '       %s\n' "$line" >&2; done
  exit 1
}

# ── environment checks ──────────────────────────────────────────────────────

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but was not found." "${@:2}"
}

check_host() {
  step "Checking this host"
  require_command curl
  require_command tar
  require_command od
  require_command docker "Install Docker Engine (https://docs.docker.com/engine/install/) or Docker Desktop."
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    die "sha256sum or shasum is required to verify the download."
  fi

  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64 | amd64 | aarch64 | arm64) ;;
    *) die "AstraBox images are published for amd64 and arm64; this host is $arch." ;;
  esac

  local compose_version compose_major
  compose_version="$(docker compose version --short 2>/dev/null)" \
    || die "The Docker Compose plugin is required: 'docker compose version' failed." \
      "Install it: https://docs.docker.com/compose/install/linux/"
  compose_version="${compose_version#v}"
  compose_major="${compose_version%%.*}"
  [[ "$compose_major" =~ ^[0-9]+$ ]] && [ "$compose_major" -ge 2 ] \
    || die "Docker Compose v2 or later is required; found $compose_version."

  local info_error
  if ! info_error="$(docker info --format '{{.ServerVersion}}' 2>&1 >/dev/null)"; then
    case "$info_error" in
      *"permission denied"*)
        die "$(id -un) cannot use the Docker socket." \
          "Add the user to the docker group and log in again:" \
          "  sudo usermod -aG docker $(id -un)" \
          "or run this installer as root." ;;
      *)
        die "The Docker daemon is not answering." "$info_error" ;;
    esac
  fi

  # The server container drives this daemon through the mounted socket to
  # create sandboxes, so the stack needs the daemon the socket path names.
  if [ -n "${DOCKER_HOST:-}" ] && [ "$DOCKER_HOST" != "unix:///var/run/docker.sock" ]; then
    die "DOCKER_HOST points at $DOCKER_HOST." \
      "AstraBox mounts /var/run/docker.sock into its server; run the installer" \
      "against the daemon that socket serves (unset DOCKER_HOST)."
  fi
  [ -S /var/run/docker.sock ] \
    || die "/var/run/docker.sock is not a Docker socket." \
      "AstraBox mounts it into its server to create sandboxes."
}

# ── release bundle ──────────────────────────────────────────────────────────

sha256_check() {
  local directory="$1" sum_file="$2"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$directory" && sha256sum --check --status "$sum_file")
  else
    (cd "$directory" && shasum -a 256 --check --status "$sum_file")
  fi
}

# GitHub redirects /releases/latest to the newest published, non-prerelease
# release's tag page. Following the redirect needs no API token and is not
# subject to the REST API's unauthenticated rate limit.
latest_version() {
  local url
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPOSITORY/releases/latest")" \
    || die "Cannot reach https://github.com/$REPOSITORY/releases/latest." \
      "Set ASTRABOX_VERSION, or ASTRABOX_INSTALL_BUNDLE for an offline installation."
  case "$url" in
    */releases/tag/v*) printf '%s\n' "${url##*/releases/tag/v}" ;;
    *) die "GitHub reports no published AstraBox release ($url)." "Set ASTRABOX_VERSION to a released version." ;;
  esac
}

# Prints the extracted bundle directory.
fetch_bundle() {
  local work="$1" archive version
  if [ -n "${ASTRABOX_INSTALL_BUNDLE:-}" ]; then
    archive="$ASTRABOX_INSTALL_BUNDLE"
    [ -f "$archive" ] || die "ASTRABOX_INSTALL_BUNDLE is not a file: $archive"
    if [ -f "$archive.sha256" ]; then
      cp "$archive" "$archive.sha256" "$work/"
      sha256_check "$work" "$(basename "$archive").sha256" \
        || die "$archive does not match $archive.sha256."
    fi
  else
    version="${ASTRABOX_VERSION:-}"
    version="${version#v}"
    [ -n "$version" ] || version="$(latest_version)"
    local name="astrabox-deploy-$version.tar.gz"
    local url="https://github.com/$REPOSITORY/releases/download/v$version/$name"
    step "Downloading AstraBox $version"
    curl -fsSL -o "$work/$name" "$url" \
      || die "Cannot download $url." "Check that v$version is a published release."
    curl -fsSL -o "$work/$name.sha256" "$url.sha256" \
      || die "Cannot download $url.sha256."
    sha256_check "$work" "$name.sha256" || die "$name does not match its published checksum."
    archive="$work/$name"
  fi
  mkdir "$work/bundle"
  # -p keeps the bundle's file modes whatever the umask: containers running as
  # other users read the mounted Compose inputs.
  tar -xpzf "$archive" -C "$work/bundle" || die "$archive is not a readable bundle."
  local roots=("$work"/bundle/astrabox-deploy-*)
  [ "${#roots[@]}" -eq 1 ] && [ -f "${roots[0]}/VERSION" ] && [ -f "${roots[0]}/containers/compose.yaml" ] \
    || die "$archive is not an AstraBox deployment bundle."
  printf '%s\n' "${roots[0]}"
}

install_bundle() {
  local bundle="$1" install_dir="$2" relative
  mkdir -p "$install_dir"
  (cd "$bundle" && find . -type f) | while IFS= read -r relative; do
    relative="${relative#./}"
    mkdir -p "$install_dir/$(dirname "$relative")"
    cp -p "$bundle/$relative" "$install_dir/$relative"
  done
}

# ── settings file ───────────────────────────────────────────────────────────
#
# Values are single-quoted: Compose then reads them literally, so a key that
# contains `$` or `#` reaches the container unchanged.

env_get() {
  local file="$1" key="$2" line value
  [ -f "$file" ] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -n 1)" || return 0
  value="${line#*=}"
  case "$value" in
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
  esac
  printf '%s' "$value"
}

env_unset() {
  local file="$1" key="$2" kept
  [ -f "$file" ] || return 0
  kept="$(grep -Ev "^${key}=" "$file" || true)"
  (umask 077 && printf '%s\n' "$kept" >"$file.tmp")
  mv "$file.tmp" "$file"
}

env_set() {
  local file="$1" key="$2" value="$3"
  case "$value" in
    *\'* | *$'\n'*) die "$key cannot contain a single quote or a line break." ;;
  esac
  env_unset "$file" "$key"
  printf "%s='%s'\n" "$key" "$value" >>"$file"
}

create_env_file() {
  local file="$1"
  [ -f "$file" ] && return 0
  (
    umask 077
    cat >"$file" <<EOF
# AstraBox installation settings. Docker Compose reads this file when it runs
# in this directory. scripts/install.sh writes the keys it manages and keeps
# every other line, so add Compose settings here and re-run the installer.
# See $DOCS_URL
EOF
  )
}

# ── secrets ─────────────────────────────────────────────────────────────────

new_secret() {
  local value
  value="$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || die "Could not read 32 random bytes from /dev/urandom."
  printf '%s\n' "$value"
}

ensure_secrets() {
  local directory="$1" postgres_volume="$2" name path value missing_database_secret=0
  [ -L "$directory" ] && die "Refusing a symlinked secret directory: $directory"
  for name in "${DATABASE_SECRET_NAMES[@]}"; do
    [ -f "$directory/$name" ] || missing_database_secret=1
  done
  # A new password file cannot open a database initialized with the old one.
  if [ "$missing_database_secret" = 1 ] && docker volume inspect "$postgres_volume" >/dev/null 2>&1; then
    die "Database passwords are missing from $directory, but the PostgreSQL volume '$postgres_volume' exists." \
      "Restore the directory from your backup, or set ASTRABOX_POSTGRES_VOLUME in the" \
      "installation's containers/.env to a new volume name for a separate deployment." \
      "See $DOCS_URL"
  fi
  mkdir -p "$directory"
  chmod 700 "$directory"
  for name in "${SECRET_NAMES[@]}"; do
    path="$directory/$name"
    if [ -L "$path" ]; then
      die "Refusing a symlinked secret file: $path"
    elif [ -e "$path" ]; then
      [ -f "$path" ] || die "Secret is not a regular file: $path"
      value="$(tr -d '\r\n' <"$path")"
      [[ "$value" =~ ^[0-9a-f]{64}$ ]] \
        || die "Secret has an invalid format: $path" "Restore its original 64-character hexadecimal value."
    else
      (umask 077 && new_secret >"$path.tmp.$$")
      mv "$path.tmp.$$" "$path"
    fi
    # Compose mounts secret files with their host mode and owner; the server
    # runs as another uid. The 0700 directory keeps them from other host users.
    chmod 604 "$path"
  done
}

# ── model service ───────────────────────────────────────────────────────────

prompt() {
  local question="$1" default="${2:-}" answer
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$question" "$default" >&2
  else
    printf '%s: ' "$question" >&2
  fi
  IFS= read -r answer <&3 || die "No answer was read from the terminal."
  printf '%s' "${answer:-$default}"
}

confirm() {
  local question="$1" answer
  printf '%s [Y/n]: ' "$question" >&2
  IFS= read -r answer <&3 || die "No answer was read from the terminal."
  case "$answer" in
    "" | [Yy]*) return 0 ;;
    *) return 1 ;;
  esac
}

prompt_secret() {
  local question="$1" answer
  printf '%s: ' "$question" >&2
  IFS= read -rs answer <&3 || die "No answer was read from the terminal."
  printf '\n' >&2
  printf '%s' "$answer"
}

valid_token() {
  [[ "$1" =~ ^[A-Za-z0-9._~:/+=-]+$ ]]
}

valid_model() {
  [[ "$1" =~ ^[A-Za-z0-9._:/-]+$ ]]
}

valid_base_url() {
  [[ "$1" =~ ^https?://[A-Za-z0-9._~:/%-]+$ ]]
}

# Sets the globals provider, model_key, model_name and model_base_url.
ask_model_settings() {
  cat >&2 <<'EOF'

Which model service should Agents use?
  1) Anthropic
  2) DeepSeek
  3) Another Anthropic-compatible service (base URL, API key and model ID)
  4) An OpenAI-compatible service (base URL, API key and model ID)
  5) None for now: add model routes later in the LiteLLM gateway
EOF
  local choice
  choice="$(prompt "Choose 1-5" "1")"
  case "$choice" in
    1) provider=anthropic ;;
    2) provider=deepseek ;;
    3) provider=anthropic-compatible ;;
    4) provider=openai-compatible ;;
    5) provider=none; return 0 ;;
    *) die "Choose a number from 1 to 5." ;;
  esac
  case "$provider" in
    anthropic-compatible | openai-compatible)
      model_base_url="$(prompt "Base URL, such as https://api.example.com/v1")" ;;
  esac
  model_key="$(prompt_secret "API key (input is hidden)")"
  case "$provider" in
    deepseek) model_name="$(prompt "Model ID, as DeepSeek names it" "$DEEPSEEK_DEFAULT_MODEL")" ;;
    *) model_name="$(prompt "Model ID, as your model service names it")" ;;
  esac
}

read_model_settings_from_environment() {
  provider="$ASTRABOX_INSTALL_MODEL_PROVIDER"
  model_key="${ASTRABOX_INSTALL_MODEL_API_KEY:-}"
  model_name="${ASTRABOX_INSTALL_MODEL_NAME:-}"
  model_base_url="${ASTRABOX_INSTALL_MODEL_BASE_URL:-}"
  if [ "$provider" = deepseek ] && [ -z "$model_name" ]; then
    model_name="$DEEPSEEK_DEFAULT_MODEL"
  fi
}

validate_model_settings() {
  case "$provider" in
    none) return 0 ;;
    anthropic | deepseek | anthropic-compatible | openai-compatible) ;;
    *) die "Unknown model provider '$provider'." \
      "Use anthropic, deepseek, anthropic-compatible, openai-compatible or none." ;;
  esac
  [ -n "$model_key" ] || die "The $provider model service needs an API key (ASTRABOX_INSTALL_MODEL_API_KEY)."
  valid_token "$model_key" || die "The API key contains characters an API key does not use."
  [ -n "$model_name" ] || die "The $provider model service needs a model ID (ASTRABOX_INSTALL_MODEL_NAME)."
  valid_model "$model_name" || die "The model ID '$model_name' contains unsupported characters."
  case "$provider" in
    anthropic-compatible | openai-compatible)
      valid_base_url "$model_base_url" \
        || die "The $provider service needs an http(s) base URL (ASTRABOX_INSTALL_MODEL_BASE_URL)." ;;
  esac
}

# Writes the settings each service is reached through in the bundled gateway
# (containers/litellm/config.yaml). ANTHROPIC_MODEL is the deployment's default
# model: the seeded Agents use it until an Agent selects its own. With an
# ANTHROPIC_BASE_URL set, the deployment serves it through the gateway's
# `anthropic/*` routes (astrabox/deploy/onebox.py).
write_model_settings() {
  local env_file="$1" key
  for key in "${MODEL_KEYS[@]}"; do env_unset "$env_file" "$key"; done
  env_set "$env_file" ASTRABOX_INSTALL_MODEL_PROVIDER "$provider"
  case "$provider" in
    anthropic)
      env_set "$env_file" ANTHROPIC_API_KEY "$model_key"
      env_set "$env_file" ANTHROPIC_MODEL "$model_name" ;;
    deepseek)
      # DeepSeek's own Anthropic Messages endpoint, which Claude Code speaks
      # natively. The deployment recognises this endpoint and gives the same
      # credential to the gateway's OpenAI-wire DeepSeek routes.
      env_set "$env_file" ANTHROPIC_BASE_URL "$DEEPSEEK_BASE_URL"
      env_set "$env_file" ANTHROPIC_API_KEY "$model_key"
      env_set "$env_file" ANTHROPIC_MODEL "$model_name" ;;
    anthropic-compatible)
      env_set "$env_file" ANTHROPIC_BASE_URL "$model_base_url"
      env_set "$env_file" ANTHROPIC_API_KEY "$model_key"
      env_set "$env_file" ANTHROPIC_MODEL "$model_name" ;;
    openai-compatible)
      env_set "$env_file" OPENAI_COMPATIBLE_BASE_URL "$model_base_url"
      env_set "$env_file" OPENAI_COMPATIBLE_API_KEY "$model_key"
      env_set "$env_file" ANTHROPIC_MODEL \
        "$OPENAI_COMPATIBLE_ROUTE_PREFIX${model_name#"$OPENAI_COMPATIBLE_ROUTE_PREFIX"}" ;;
  esac
}

configure_model() {
  local env_file="$1" current
  current="$(env_get "$env_file" ASTRABOX_INSTALL_MODEL_PROVIDER)"
  provider="" model_key="" model_name="" model_base_url=""
  if [ -n "${ASTRABOX_INSTALL_MODEL_PROVIDER:-}" ]; then
    read_model_settings_from_environment
  elif [ "$have_tty" = 1 ]; then
    if [ -n "$current" ] && confirm "Keep the current model service ($current)?"; then
      return 0
    fi
    ask_model_settings
  elif [ -n "$current" ]; then
    return 0
  else
    die "No terminal is available to ask which model service to use." \
      "Set ASTRABOX_INSTALL_MODEL_PROVIDER and, for a provider other than none," \
      "ASTRABOX_INSTALL_MODEL_API_KEY and ASTRABOX_INSTALL_MODEL_NAME." \
      "See $DOCS_URL"
  fi
  validate_model_settings
  write_model_settings "$env_file"
}

# ── stack ───────────────────────────────────────────────────────────────────

compose() {
  (cd "$install_dir/containers" && docker compose "$@")
}

socket_group() {
  stat -L -c %g /var/run/docker.sock 2>/dev/null || stat -L -f %g /var/run/docker.sock
}

bridge_gateway() {
  local gateway
  gateway="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null)" \
    || die "Docker has no default bridge network." "OpenSandbox attaches every sandbox to it."
  [[ "$gateway" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || die "Cannot read the default bridge network's gateway address ('$gateway')."
  printf '%s' "$gateway"
}

# `restarting` or `exited` is a server that fails at startup and is restarted by
# its policy: the readiness poll would otherwise keep waiting on a container
# that prints the reason on every attempt.
server_state() {
  local container
  container="$(compose ps -q server 2>/dev/null | head -1)"
  [ -n "$container" ] || { printf 'missing'; return; }
  docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || printf 'missing'
}

http_status() {
  curl -s -o "${2:-/dev/null}" -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || true
}

show_failure_evidence() {
  printf '\n--- docker compose ps ---\n' >&2
  compose ps >&2 || true
  printf '\n--- server log (last 60 lines) ---\n' >&2
  compose logs --no-color --tail 60 server 2>&1 \
    | grep -Eiv 'api_key|auth_token|secret|password' >&2 || true
}

wait_until_ready() {
  local base_url="$1" deadline page state failed_states=0
  deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
  page="$(mktemp)"
  step "Waiting for AstraBox to answer at $base_url"
  until [ "$(http_status "$base_url/healthz")" = 200 ]; do
    state="$(server_state)"
    case "$state" in
      running) failed_states=0 ;;
      # Two in a row, so a restart between two polls is not called a failure.
      *) failed_states=$((failed_states + 1)) ;;
    esac
    if [ "$failed_states" -ge 2 ]; then
      show_failure_evidence
      die "The AstraBox server container is $state; its log above says why."
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      show_failure_evidence
      die "AstraBox did not report healthy within $READY_TIMEOUT_SECONDS seconds."
    fi
    sleep 5
  done
  # Health is the API process; the console is the page a user opens.
  until [ "$(http_status "$base_url/" "$page")" = 200 ] && grep -q '<title>AstraBox</title>' "$page"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      show_failure_evidence
      die "The console page was not served within $READY_TIMEOUT_SECONDS seconds."
    fi
    sleep 5
  done
  rm -f "$page"
}

# A Session's first sandbox would otherwise wait on this multi-gigabyte pull.
# The server names the image, so the installer pulls exactly what it will run.
pull_agent_image() {
  local image
  image="$(compose exec -T server python -c \
    'from astrabox.providers.sandbox_image import resolve_agent_image; print(resolve_agent_image())' \
    </dev/null | tr -d '\r')" || die "Cannot ask the server which Agent sandbox image it runs."
  [ -n "$image" ] || die "The server reported no Agent sandbox image."
  step "Pulling the Agent sandbox image $image"
  docker pull --quiet "$image" </dev/null >/dev/null || die "Cannot pull $image."
}

# ── main ────────────────────────────────────────────────────────────────────

main() {
  check_host

  # `curl ... | bash` gives the script as stdin, so questions are read from
  # the terminal on descriptor 3.
  have_tty=0
  if { exec 3</dev/tty; } 2>/dev/null; then
    have_tty=1
  fi

  install_dir="${ASTRABOX_INSTALL_DIR:-$HOME/astrabox}"
  mkdir -p "$install_dir"
  install_dir="$(cd "$install_dir" && pwd)"
  local env_file="$install_dir/containers/.env"
  local previous_version=""
  [ -f "$install_dir/VERSION" ] && previous_version="$(tr -d '\r\n' <"$install_dir/VERSION")"

  local bundle version
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  bundle="$(fetch_bundle "$work")"
  version="$(tr -d '\r\n' <"$bundle/VERSION")"
  if [ -n "${ASTRABOX_VERSION:-}" ] && [ "${ASTRABOX_VERSION#v}" != "$version" ]; then
    die "ASTRABOX_VERSION is $ASTRABOX_VERSION, but the bundle is AstraBox $version."
  fi
  if [ -n "$previous_version" ]; then
    step "Upgrading AstraBox $previous_version to $version in $install_dir"
  else
    step "Installing AstraBox $version in $install_dir"
  fi
  install_bundle "$bundle" "$install_dir"

  create_env_file "$env_file"
  chmod 600 "$env_file"
  [ -n "$(env_get "$env_file" COMPOSE_PROJECT_NAME)" ] || env_set "$env_file" COMPOSE_PROJECT_NAME astrabox
  [ -z "${ASTRABOX_IMAGE_PREFIX:-}" ] || env_set "$env_file" ASTRABOX_IMAGE_PREFIX "$ASTRABOX_IMAGE_PREFIX"
  env_set "$env_file" ASTRABOX_IMAGE_TAG "$version"
  local docker_gid gateway
  docker_gid="$(socket_group)" || die "Cannot read the group of /var/run/docker.sock."
  env_set "$env_file" DOCKER_GID "$docker_gid"
  if [ -z "$(env_get "$env_file" ASTRABOX_DOCKER_BRIDGE_GATEWAY)" ]; then
    gateway="$(bridge_gateway)"
    env_set "$env_file" ASTRABOX_DOCKER_BRIDGE_GATEWAY "$gateway"
  fi

  local secret_dir postgres_volume
  secret_dir="$(env_get "$env_file" ASTRABOX_LOCAL_DATABASE_SECRET_DIR)"
  secret_dir="${secret_dir:-$install_dir/.astrabox/database-secrets}"
  postgres_volume="$(env_get "$env_file" ASTRABOX_POSTGRES_VOLUME)"
  step "Preparing secrets in $secret_dir"
  ensure_secrets "$secret_dir" "${postgres_volume:-astrabox-postgres}"

  configure_model "$env_file"

  compose config --quiet || die "Docker Compose rejected the configuration in $install_dir/containers."
  step "Pulling the AstraBox $version images"
  compose pull --quiet || die "Cannot pull the AstraBox $version images." \
    "Check the registry named by ASTRABOX_IMAGE_PREFIX in $env_file, if set."
  step "Starting AstraBox"
  compose up -d --no-build --remove-orphans \
    || { show_failure_evidence; die "Docker Compose could not start the stack."; }

  local port base_url
  port="$(env_get "$env_file" ASTRABOX_SERVER_HOST_PORT)"
  base_url="http://127.0.0.1:${port:-8088}"
  wait_until_ready "$base_url"
  pull_agent_image

  cat <<EOF

AstraBox $version is running: $base_url

  Open it in a browser on this host. It listens on loopback only and has no
  login; from another computer, tunnel to it:
    ssh -L ${port:-8088}:127.0.0.1:${port:-8088} <this-host>
  Configure team login before exposing it: $TEAM_LOGIN_DOCS_URL

  Settings:  $env_file
  Secrets:   $secret_dir
             Back it up together with the Docker volumes.
  Upgrade:   run this installer again.
  Manage:    cd $install_dir/containers
             docker compose ps
             docker compose logs -f server
             docker compose down      (keeps data; add -v to delete it)
EOF
  if [ "$(env_get "$env_file" ASTRABOX_INSTALL_MODEL_PROVIDER)" = none ]; then
    printf '\n  No model service is configured. Add one in the console under\n'
    printf '  Integrated services > LiteLLM gateway, or re-run the installer.\n'
    printf '  %s\n' "$MODELS_DOCS_URL"
  fi
}

# Sourcing the file defines the functions without installing anything. When
# the file arrives through a pipe, bash reads this whole `if` before running
# it, so a download cut short never runs part of the installer.
if ! (return 0 2>/dev/null); then
  main "$@"
fi
