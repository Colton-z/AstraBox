#!/usr/bin/env bash
# Build the deployment bundle that scripts/install.sh installs.
#
#   scripts/build-release-bundle.sh OUTPUT_DIR
#
# Writes OUTPUT_DIR/astrabox-deploy-<version>.tar.gz and its .sha256 file. The
# archive holds the Compose stack exactly as containers/ lays it out, so the
# relative paths inside containers/compose.yaml resolve the same way in an
# installation as in a checkout; it holds no source, because an installation
# pulls the published images. The version is pyproject.toml's, the value the
# release workflow checks the tag against and tags every image with.
set -euo pipefail

[ "$#" -eq 1 ] || { printf 'usage: %s OUTPUT_DIR\n' "$0" >&2; exit 64; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly repo_root
readonly output_dir="$1"
version="$(grep -m1 '^version = ' "$repo_root/pyproject.toml" | cut -d'"' -f2)"
readonly version
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] \
  || { printf 'pyproject.toml has no usable version: %q\n' "$version" >&2; exit 65; }

# The licence files and every file containers/compose.yaml mounts by a
# relative path. tests/release_bundle_test.py fails when compose.yaml mounts a
# file this list lacks.
readonly bundle_files=(
  LICENSE
  NOTICE
  containers/compose.yaml
  containers/postgres/init-databases.sh
  containers/coredns/sandbox-edge.Corefile
  containers/sandbox-edge/default.conf.template
)

readonly name="astrabox-deploy-$version"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/$name"
for path in "${bundle_files[@]}"; do
  [ -f "$repo_root/$path" ] || { printf 'bundle input is missing: %s\n' "$path" >&2; exit 66; }
  # Fixed modes rather than the checkout's, which follow the local umask:
  # containers that run as other users read these files through bind mounts.
  install -D -m 0644 "$repo_root/$path" "$staging/$name/$path"
done
printf '%s\n' "$version" >"$staging/$name/VERSION"
chmod 0644 "$staging/$name/VERSION"

mkdir -p "$output_dir"
readonly archive="$output_dir/$name.tar.gz"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' \
  -C "$staging" -czf "$archive" "$name"
(cd "$output_dir" && sha256sum "$name.tar.gz" >"$name.tar.gz.sha256")
printf '%s\n' "$archive"
