#!/usr/bin/env bash
#
# Patch AIO python-server (port 8080) to run as root with a caller-chosen
# workspace root. Single source of truth shared between the sandbox runtime
# images (containers/sandbox-claude-code/Dockerfile and
# containers/sandbox-hermes/Dockerfile) — keep both images in sync by
# only editing this script.
#
# Reason: AIO base image runs python-server (which serves /v1/file/* and
# /v1/shell/exec — the engine-neutral file API the platform calls) as an
# unprivileged service user, so it cannot read into per-user conversation dirs
# (700) or root-only paths. Both images require root here; user isolation is
# enforced upstream by SessionFileService root_path scoping, not at the
# python-server filesystem layer.
#
# Usage:
#   bash patch-aio-python-server.sh <workspace>
#
# Workspace argument:
#   /                    — Claude Code runtime; conversation paths are arbitrary
#                          (under /root, /home/<conv_user>, etc.) so workspace
#                          must be broad enough to cover all of them.
#   /home/conversations  — Hermes runtime; per-(user, assistant) profiles all
#                          live under this prefix.
#
# Idempotent: re-running on an already-patched conf leaves it unchanged. The
# final grep prints the resulting key lines so build logs show the effect.
set -eux

if [ "$#" -ne 1 ] || [ -z "${1:-}" ]; then
  echo "usage: $0 <workspace-path>" >&2
  exit 64
fi

workspace="$1"
py_srv_conf=/opt/gem/supervisord/supervisord.python_srv.conf

if [ ! -f "$py_srv_conf" ]; then
  echo "patch-aio-python-server: $py_srv_conf not found in this image" >&2
  exit 65
fi

# `--workspace [^[:space:]]*` matches both the unpatched default and any prior
# workspace value, so re-applying the patch with a different workspace is safe.
sed -i \
    -e 's|^user=%(ENV_USER)s$|user=root|' \
    -e 's|HOME="/home/%(ENV_USER)s"|HOME="/root"|' \
    -e 's|USER="%(ENV_USER)s"|USER="root"|' \
    -e "s|--workspace [^[:space:]]*|--workspace ${workspace}|" \
    "$py_srv_conf"

grep -E '^(user|environment|command)=' "$py_srv_conf"
