# syntax=docker/dockerfile:1
#
# AstraBox — in-sandbox Hermes runtime image (per-user "assistant" engine).
#
# This is the container the sandbox backend spawns for an assistant (per-user)
# workspace, the counterpart to the per-session claude-code image. It bakes the
# public MIT-licensed Hermes agent (`hermes-agent`) into an image-owned
# virtualenv. AstraBox starts the official TUI JSON-RPC entry point for each
# conversation through OpenSandbox execd; no Hermes application port is opened.
# Per-user HERMES_HOME/profile/session state is supplied at runtime, not baked in.
#
# Build (repo-root context):
#   docker build -t astrabox/sandbox-hermes:latest -f containers/sandbox-hermes/Dockerfile .
#
# Base image: the same PUBLIC AIO Sandbox base as the Claude Code image. Its
# entrypoint creates the workload account and keeps the base lifecycle alive.
# OpenSandbox's native execd and Filesystem APIs provide commands and file
# transport. Conversation-scoped Hermes processes start later through execd PTY.
ARG SANDBOX_BASE_IMAGE=ghcr.io/agent-infra/sandbox:1.11.0
FROM ${SANDBOX_BASE_IMAGE}

# Official release source retains the supplier's bundled assets beside its Python
# modules. The archive checksum and installed version must match the release.
ARG HERMES_AGENT_VERSION=0.21.0
ARG HERMES_SOURCE_COMMIT=29112bef099274229cadff79cdff7bf7b99c4b77
ARG HERMES_SOURCE_SHA256=76b99a8be9b77d66833c3cfe2b35c6d6f6a58e4ff9637ef8effcfc1f420ab35a
ARG HERMES_EXTRAS=all
ARG HERMES_VENV=/opt/hermes/.venv
# Optional memory-provider client(s) to pre-bake for fast first-use, as a
# pip-installable spec (e.g. "openviking"). Empty by default: the image stays
# plugin-neutral (Hermes runs with no external memory provider, and providers in
# hermes lazy_deps install on demand). A deployment that standardizes on a
# provider sets this to bake its client — the memory provider itself is still
# selected per-template via model_config.hermes.plugins, never in the image.
ARG HERMES_MEMORY_PROVIDER_PIP=

USER root

ENV HERMES_VENV=${HERMES_VENV} \
    ASTRABOX_HERMES_AUTOSTART=false \
    BOOTSTRAP_CMD="bash -c '/usr/local/bin/astrabox-hermes-bootstrap'"

# Keep the vendor source and virtualenv outside the mutable profile home.
RUN set -eux; \
    curl -fsSL "https://github.com/NousResearch/hermes-agent/archive/${HERMES_SOURCE_COMMIT}.tar.gz" \
      -o /tmp/astrabox-hermes-source.tar.gz; \
    printf '%s  %s\n' "${HERMES_SOURCE_SHA256}" /tmp/astrabox-hermes-source.tar.gz | sha256sum -c -; \
    mkdir -p /opt/hermes/source; \
    tar -xzf /tmp/astrabox-hermes-source.tar.gz --strip-components=1 --no-same-owner -C /opt/hermes/source; \
    rm /tmp/astrabox-hermes-source.tar.gz; \
    uv venv --python python3.11 "${HERMES_VENV}"; \
    if [ -n "${HERMES_EXTRAS}" ]; then \
      HERMES_SPEC="/opt/hermes/source[${HERMES_EXTRAS}]"; \
    else \
      HERMES_SPEC=/opt/hermes/source; \
    fi; \
    uv pip install --python "${HERMES_VENV}/bin/python" --no-cache -e "${HERMES_SPEC}"; \
    ln -sf "${HERMES_VENV}/bin/hermes" /usr/local/bin/hermes; \
    "${HERMES_VENV}/bin/python" -c 'import importlib.metadata, sys; assert importlib.metadata.version("hermes-agent") == sys.argv[1]' "${HERMES_AGENT_VERSION}"; \
    command -v hermes; \
    hermes --version; \
    hermes serve --help; \
    hermes backup --help; \
    hermes import --help; \
    "${HERMES_VENV}/bin/python" -c 'import tui_gateway.entry'

# Optionally pre-bake a memory-provider client (see HERMES_MEMORY_PROVIDER_PIP).
# Empty → plugin-neutral image: Hermes runs with no external memory provider, and
# providers in hermes lazy_deps install on demand at first use given sandbox egress.
RUN set -eux; \
    if [ -n "${HERMES_MEMORY_PROVIDER_PIP}" ]; then \
      uv pip install --python "${HERMES_VENV}/bin/python" --no-cache ${HERMES_MEMORY_PROVIDER_PIP}; \
    fi

# Generic profile config.yaml merger — the single contract between host and image.
COPY scripts/runtime/hermes_config_merge.py /usr/local/bin/astrabox-hermes-config-merge
RUN set -eux; \
    chmod +x /usr/local/bin/astrabox-hermes-config-merge; \
    mkdir -p /opt/astrabox/hermes-skills; \
    chmod 755 /opt/astrabox/hermes-skills

# Identity provisioning is a fixed platform capability. Bake it before a box
# can enter either prewarm layer; a conversation claim only triggers it with
# profile-specific values.
COPY astrabox/core/service/orchestrator/runtime/provision-conversation /usr/local/bin/astrabox-provision-conversation
RUN chmod 0555 /usr/local/bin/astrabox-provision-conversation && \
    bash -n /usr/local/bin/astrabox-provision-conversation

COPY astrabox/core/service/orchestrator/runtime/astrabox-assistant-workspace-storage /usr/local/bin/astrabox-assistant-workspace-storage
COPY astrabox/core/service/orchestrator/runtime/hermes-profile-setup /usr/local/bin/astrabox-hermes-profile-setup
COPY astrabox/core/service/orchestrator/runtime/hermes-skill-repo-cache /usr/local/bin/astrabox-hermes-skill-repo-cache
RUN chmod 0555 /usr/local/bin/astrabox-assistant-workspace-storage && \
    chmod 0555 /usr/local/bin/astrabox-hermes-profile-setup && \
    chmod 0555 /usr/local/bin/astrabox-hermes-skill-repo-cache && \
    python3 -c "import ast; ast.parse(open('/usr/local/bin/astrabox-hermes-skill-repo-cache').read())" && \
    bash -n /usr/local/bin/astrabox-assistant-workspace-storage && \
    bash -n /usr/local/bin/astrabox-hermes-profile-setup

# Root the in-box python-server workspace at /home/conversations — the prefix
# under which every per-(user, assistant) Hermes profile (mode 700) lives.
COPY scripts/runtime/patch-aio-python-server.sh /usr/local/bin/astrabox-patch-aio-python-server
RUN set -eux; \
    chmod +x /usr/local/bin/astrabox-patch-aio-python-server; \
    bash /usr/local/bin/astrabox-patch-aio-python-server /home/conversations

# Hermes' headless backend, as a service of this image.
#
# `hermes serve` fronts the same `tui_gateway.server.dispatch` the TUI speaks
# over stdio — the wire is identical, so nothing about the host's event
# translation depends on which one it dials. What the image owning it buys is
# in `docs/maintainers/hermes-transport.md`: no PTY (so Hermes does not
# decorate its reasoning stream for a terminal it thinks it has), and an engine
# that comes back with a resumed box instead of with the next user's first
# message.
# socat publishes the loopback-bound backend on the sandbox's outward port;
# astrabox-hermes-serve explains why the backend does not bind outward itself.
# A stock TCP proxy forwards bytes unchanged, so the WebSocket upgrade and its
# `?token=` credential pass through without the proxy having to understand
# either. The base image does not carry it — the build guard below is what
# said so, at build time rather than in a box.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends socat; \
    rm -rf /var/lib/apt/lists/*

COPY containers/sandbox-hermes/astrabox-hermes-serve /usr/local/bin/astrabox-hermes-serve
COPY containers/sandbox-hermes/astrabox-hermes-forward /usr/local/bin/astrabox-hermes-forward
COPY containers/sandbox-hermes/hermes_native_store.py containers/sandbox-hermes/hermes_state_transport.py containers/sandbox-hermes/hermes_state_bootstrap.py containers/sandbox-hermes/hermes_state_mirror.py /opt/astrabox/hermes/
COPY astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror /usr/local/bin/astrabox-transcript-mirror
COPY containers/sandbox-hermes/supervisord.hermes.conf /opt/gem/supervisord/supervisord.hermes.conf
RUN set -eux; \
    chmod 0555 /usr/local/bin/astrabox-hermes-serve; \
    chmod 0555 /usr/local/bin/astrabox-hermes-forward; \
    chmod 0555 /usr/local/bin/astrabox-transcript-mirror; \
    bash -n /usr/local/bin/astrabox-hermes-serve; \
    bash -n /usr/local/bin/astrabox-hermes-forward; \
    "${HERMES_VENV}/bin/python" -c 'import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("/opt/astrabox/hermes").glob("*.py")]'; \
    "${HERMES_VENV}/bin/python" -c 'import ast, pathlib; ast.parse(pathlib.Path("/usr/local/bin/astrabox-transcript-mirror").read_text())'; \
    PYTHONPATH=/opt/astrabox/hermes "${HERMES_VENV}/bin/python" -c 'import hermes_native_store, hermes_state_transport, hermes_state_bootstrap, hermes_state_mirror'; \
    supervisord --version; \
    command -v socat >/dev/null

# Bootstrap only the base sandbox services; the backend above is supervised.
RUN set -eux; \
    { \
      printf '%s\n' '#!/usr/bin/env bash'; \
      printf '%s\n' 'set -euo pipefail'; \
      printf '%s\n' ''; \
      printf '%s\n' 'exec /opt/gem/run.sh'; \
    } > /usr/local/bin/astrabox-hermes-bootstrap; \
    chmod +x /usr/local/bin/astrabox-hermes-bootstrap

WORKDIR /home/conversations
