#!/usr/bin/env bash
# Turn a fresh Ubuntu 24.04 machine into the AstraBox cloud image.
#
#   sudo ASTRABOX_VERSION=0.1.14 ./build.sh && sudo ./cleanup.sh
#
# then create a machine image from the stopped machine. Packer runs the same
# two scripts (cloud/packer); on a cloud Packer does not cover, run them by hand.
#
# The image holds everything a first boot needs, so a machine that cannot reach
# GitHub or the container registries still starts AstraBox:
#   * Docker Engine and the Compose plugin;
#   * one release's deployment bundle and the installer, in /opt/astrabox-image;
#   * every container image a fresh installation runs. They are collected by
#     installing that release once, with team login, and removing the
#     installation again: its containers, volumes, secrets and settings;
#   * the Caddy image of the HTTPS edge;
#   * astrabox-first-boot and its systemd unit, which install AstraBox with
#     this machine's own secrets and address when a machine boots from the image.
#
# Settings (all optional):
#   ASTRABOX_VERSION          release to bake, such as 0.1.14; default: latest
#   ASTRABOX_INSTALL_BUNDLE   a downloaded astrabox-deploy-<version>.tar.gz
#   ASTRABOX_IMAGE_PREFIX     registry mirror holding the AstraBox images; the
#                             image keeps it, so later upgrades use it too
#   DOCKER_INSTALL_MIRROR     get.docker.com mirror: Aliyun or AzureChinaCloud
#   DOCKER_REGISTRY_MIRRORS   comma-separated Docker Hub mirrors, kept in the
#                             image's /etc/docker/daemon.json
#   ASTRABOX_WARM_ENGINES     sandbox images to bake besides Claude Code's,
#                             space-separated: codex pi deepseek-harness hermes.
#                             Default: none; another engine's image is pulled
#                             the first time a RuntimeProfile uses it.
set -euo pipefail

readonly REPOSITORY="Colton-z/AstraBox"
readonly IMAGE_DIR=/opt/astrabox-image
readonly WARMUP_DIR=/opt/astrabox-warmup
# The HTTPS edge. Caddy 2.11 asks Let's Encrypt for IP address certificates
# (its short-lived profile); earlier releases refuse IP addresses.
readonly CADDY_IMAGE="caddy:2.11.9@sha256:0c2243efcf437ab7f7a978f1027fcf0d4f9be8a75df27b1b3fbc5dbe0ffa8cf0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

step() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The installer is the repository's own, next to this script when Packer
# uploads both, or in the checkout this script belongs to.
find_installer() {
  local candidate
  installer=""
  for candidate in "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/../../scripts/install.sh"; do
    [ -f "$candidate" ] && { installer="$candidate"; break; }
  done
  [ -n "$installer" ] || die "install.sh is neither next to this script nor in ../../scripts."
  grep -q 'ASTRABOX_INSTALL_PULL' "$installer" \
    || die "$installer predates ASTRABOX_INSTALL_PULL, which the first boot relies on."
}

install_packages() {
  step "Updating the operating system"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get -q -y -o Dpkg::Options::=--force-confold upgrade
  apt-get -q -y install ca-certificates curl tar coreutils iproute2
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    step "Installing Docker Engine"
    local script
    script="$(mktemp)"
    curl -fsSL https://get.docker.com -o "$script"
    if [ -n "${DOCKER_INSTALL_MIRROR:-}" ]; then
      sh "$script" --mirror "$DOCKER_INSTALL_MIRROR"
    else
      sh "$script"
    fi
    rm -f "$script"
  fi
  # Rotated container logs, so a long-running machine does not fill its disk.
  mkdir -p /etc/docker
  local mirrors=""
  if [ -n "${DOCKER_REGISTRY_MIRRORS:-}" ]; then
    local mirror
    IFS=, read -r -a mirror_list <<<"$DOCKER_REGISTRY_MIRRORS"
    for mirror in "${mirror_list[@]}"; do
      mirrors="${mirrors:+$mirrors, }\"$mirror\""
    done
  fi
  cat >/etc/docker/daemon.json <<EOF
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}${mirrors:+,
  \"registry-mirrors\": [$mirrors]}
}
EOF
  systemctl enable docker >/dev/null 2>&1 || true
  systemctl restart docker
  docker info >/dev/null || die "Docker is not answering."
}

latest_version() {
  local url
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPOSITORY/releases/latest")"
  case "$url" in
    */releases/tag/v*) printf '%s' "${url##*/releases/tag/v}" ;;
    *) die "GitHub reports no published release ($url); set ASTRABOX_VERSION." ;;
  esac
}

fetch_bundle() {
  mkdir -p "$IMAGE_DIR"
  rm -f "$IMAGE_DIR"/astrabox-deploy-*.tar.gz "$IMAGE_DIR"/astrabox-deploy-*.tar.gz.sha256
  if [ -n "${ASTRABOX_INSTALL_BUNDLE:-}" ]; then
    cp "$ASTRABOX_INSTALL_BUNDLE" "$IMAGE_DIR/"
    [ -f "$ASTRABOX_INSTALL_BUNDLE.sha256" ] && cp "$ASTRABOX_INSTALL_BUNDLE.sha256" "$IMAGE_DIR/"
  else
    local version="${ASTRABOX_VERSION:-}"
    version="${version#v}"
    [ -n "$version" ] || version="$(latest_version)"
    local name="astrabox-deploy-$version.tar.gz"
    local url="https://github.com/$REPOSITORY/releases/download/v$version/$name"
    step "Downloading the AstraBox $version bundle"
    curl -fsSL -o "$IMAGE_DIR/$name" "$url"
    curl -fsSL -o "$IMAGE_DIR/$name.sha256" "$url.sha256"
    (cd "$IMAGE_DIR" && sha256sum --check --status "$name.sha256") || die "$name does not match its checksum."
  fi
  install -m 0755 "$installer" "$IMAGE_DIR/install.sh"
  # The registry mirror the images were pulled from; the first boot installs
  # with the same names.
  if [ -n "${ASTRABOX_IMAGE_PREFIX:-}" ]; then
    printf "ASTRABOX_IMAGE_PREFIX='%s'\n" "$ASTRABOX_IMAGE_PREFIX" >"$IMAGE_DIR/image.env"
  else
    rm -f "$IMAGE_DIR/image.env"
  fi
}

# Docker Hub limits anonymous pulls per address, which a shared build network
# reaches; Google's mirror of Docker Hub serves the same images, tagged here
# under their Docker Hub names so the server finds them.
pull_image() {
  local image="$1" first hub
  docker pull --quiet "$image" >/dev/null && return 0
  first="${image%%/*}"
  if [[ "$image" == */* ]] && [[ "$first" == *.* || "$first" == *:* || "$first" == localhost ]]; then
    die "Cannot pull $image."
  fi
  hub="$image"
  [[ "$image" == */* ]] || hub="library/$image"
  docker pull --quiet "mirror.gcr.io/$hub" >/dev/null || die "Cannot pull $image."
  docker tag "mirror.gcr.io/$hub" "$image"
  docker rmi "mirror.gcr.io/$hub" >/dev/null
}

bundle_path() {
  local -a bundles=("$IMAGE_DIR"/astrabox-deploy-*.tar.gz)
  [ "${#bundles[@]}" -eq 1 ] || die "Expected one bundle in $IMAGE_DIR."
  printf '%s' "${bundles[0]}"
}

# Installs the release once, as a first boot will, so that exactly the images it
# runs are on the machine. Nothing else of this installation is kept. A fresh
# build machine holds none of the images; on a second run the ones already
# pulled are kept.
warm_images() {
  step "Installing AstraBox once to collect its images"
  rm -rf "$WARMUP_DIR"
  ASTRABOX_INSTALL_BUNDLE="$(bundle_path)" \
    ASTRABOX_INSTALL_DIR="$WARMUP_DIR" \
    ASTRABOX_INSTALL_PULL=missing \
    ASTRABOX_INSTALL_TEAM_LOGIN=casdoor \
    ASTRABOX_INSTALL_MODEL_PROVIDER=none \
    bash "$IMAGE_DIR/install.sh" </dev/null
  local version prefix engine
  version="$(tr -d '\r\n' <"$WARMUP_DIR/VERSION")"
  prefix="${ASTRABOX_IMAGE_PREFIX:-ghcr.io/colton-z/astrabox-}"
  # The server starts these beside each sandbox, so the installation itself
  # never pulls them; the running server names the ones it will use.
  local runtime_images image
  runtime_images="$(cd "$WARMUP_DIR/containers" && docker compose exec -T server python -c '
import os
from astrabox.config.release_images import release_image
from astrabox.deploy.sandbox_server import DEFAULT_EGRESS_IMAGE, execd_image
print(execd_image())
print(os.environ.get("ASTRABOX_SANDBOX_EGRESS_IMAGE") or DEFAULT_EGRESS_IMAGE)
print(os.environ.get("ASTRABOX_WORKSPACE_MOUNTER_IMAGE") or release_image("workspace-mounter"))
' </dev/null 2>/dev/null | tr -d '\r')" || die "Cannot ask the server which sandbox images it runs."
  while IFS= read -r image; do
    [ -n "$image" ] || continue
    step "Pulling $image"
    pull_image "$image"
  done <<<"$runtime_images"
  for engine in ${ASTRABOX_WARM_ENGINES:-}; do
    step "Pulling the $engine sandbox image"
    pull_image "${prefix}sandbox-$engine:$version"
  done
  step "Removing the collecting installation"
  (cd "$WARMUP_DIR/containers" && docker compose down --volumes --remove-orphans)
  # Sandboxes and their edges are started by the server, outside Compose.
  local leftover
  leftover="$(docker ps -aq)"
  [ -z "$leftover" ] || docker rm -f $leftover >/dev/null
  docker volume prune --all --force >/dev/null
  docker network prune --force >/dev/null
  rm -rf "$WARMUP_DIR"
}

install_edge_image() {
  step "Pulling the HTTPS edge image"
  local image="$CADDY_IMAGE"
  # Docker Hub limits anonymous pulls per address, which a shared build
  # network reaches; Google's mirror of Docker Hub serves the same digest.
  if ! docker pull --quiet "$image" >/dev/null; then
    image="mirror.gcr.io/library/$CADDY_IMAGE"
    docker pull --quiet "$image" >/dev/null || die "Cannot pull $CADDY_IMAGE."
  fi
  printf '%s\n' "$image" >"$IMAGE_DIR/caddy-image"
}

install_first_boot() {
  step "Installing the first-boot service"
  cp -R "$SCRIPT_DIR/rootfs/." /
  chmod 0755 /usr/local/sbin/astrabox-first-boot /usr/local/sbin/astrabox-cloud /etc/update-motd.d/90-astrabox
  mkdir -p /etc/astrabox
  chmod 0700 /etc/astrabox
  rm -rf /var/lib/astrabox-cloud
  systemctl daemon-reload
  systemctl enable astrabox-first-boot.service
}

main() {
  [ "$(id -u)" = 0 ] || die "Run as root."
  find_installer
  install_packages
  install_docker
  fetch_bundle
  warm_images
  install_edge_image
  install_first_boot
  step "Built. Images on this machine:"
  docker image ls --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
  step "Run cleanup.sh, then stop the machine and create the image."
}

# Sourcing the file defines the functions without building anything.
if ! (return 0 2>/dev/null); then
  main "$@"
fi
