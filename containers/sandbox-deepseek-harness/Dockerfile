# syntax=docker/dockerfile:1
#
# AstraBox — in-sandbox DeepSeek Harness image (one API server per box).
#
# The container the sandbox backend spawns for a deepseek_harness session: the
# counterpart of the claude-code and hermes images. It installs the vendor's
# CLI (`@deepseek-ai/dsh`, MIT) from npm the way the vendor installs it, and
# registers its own `--profile web` server as a supervised service of the base
# image. AstraBox composes nothing: what the harness IS — its tools, its
# prompt, its modes — is the vendor's product decision, and the profile is
# theirs. The adapter reaches the server's /api gateway over HTTP and its two
# WebSocket downlinks; no per-conversation process is started, and no
# per-session state is baked in.
#
# Build (repo-root context):
#   docker build -t astrabox/sandbox-deepseek-harness:latest \
#     -f containers/sandbox-deepseek-harness/Dockerfile .
#
# Base image: the same PUBLIC AIO Sandbox base as the other engine images. Its
# entrypoint creates the workload account (`agent`, /home/agent/workspace) the
# adapter's runtime profile declares, and ships the Node.js runtime the CLI
# needs; OpenSandbox's native execd and Filesystem APIs provide command and
# file transport.
# Both ARGs precede the first FROM: an ARG between FROMs is scoped to the
# stage above it, and the second FROM would then see its base name blank.
ARG SANDBOX_EXECD_IMAGE=opensandbox/execd:v1.1.0
ARG SANDBOX_BASE_IMAGE=ghcr.io/agent-infra/sandbox:1.11.0
FROM ${SANDBOX_EXECD_IMAGE} AS execd
FROM ${SANDBOX_BASE_IMAGE}

# The package manifest and lockfile are the build-time contract with the vendor.
# The umbrella package and its release-family subpackages use semver ranges, so
# pinning only the umbrella package can install a mixed release. `npm ci` makes
# every image resolve the exact reviewed graph instead of the registry state on
# its build date.
ARG DSH_PREFIX=/opt/dsh

USER root

# OpenSandbox injects execd and bootstrap through every standard lifecycle
# create, including creates issued by the official SDK client-pool creator.
# The pinned image copies below are not the prewarming mechanism. Bubblewrap and
# the image-owned shared isolation policy remain required for shared tenancy;
# its allowlist names /home/conversations, and one file for every image keeps
# per-conversation write roots from drifting between engines.
COPY --from=execd /execd /execd
COPY --from=execd /bootstrap.sh /bootstrap.sh
COPY containers/sandbox-claude-code/execd-isolation.toml \
     /opt/astrabox/opensandbox/isolation.toml
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends bubblewrap; \
    rm -rf /var/lib/apt/lists/*; \
    bwrap --version; \
    mkdir -p /opt/astrabox/opensandbox; \
    ln /execd /opt/astrabox/opensandbox/execd; \
    ln /bootstrap.sh /opt/astrabox/opensandbox/bootstrap.sh; \
    chmod 0555 /execd /bootstrap.sh; \
    chmod 0444 /opt/astrabox/opensandbox/isolation.toml; \
    /opt/astrabox/opensandbox/execd --help 2>&1 | grep -q 'OpenSandbox Execd'

# socat publishes the loopback-bound API server on the sandbox's outward
# port; astrabox-dsh-serve explains why the server does not bind outward
# itself. A stock TCP proxy forwards bytes unchanged, so the harness's caller
# check still sees the Host the client sent, and the WebSocket connections
# pass through without the proxy having to understand them.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends socat; \
    rm -rf /var/lib/apt/lists/*

# Installed under a prefix AstraBox owns rather than the base image's global
# node prefix: the CLI brings ~530 packages and must not be able to shadow a
# base-image tool the sandbox's own services resolve from PATH. Resolving this
# graph exceeds npm's default V8 heap in a clean image, so the build grants npm
# a bounded larger heap without carrying that setting into the runtime.
COPY containers/sandbox-deepseek-harness/package.json \
     containers/sandbox-deepseek-harness/package-lock.json \
     ${DSH_PREFIX}/
RUN set -eux; \
    NODE_OPTIONS=--max-old-space-size=8192 \
      npm ci --prefix "${DSH_PREFIX}" --omit=dev --no-audit --no-fund; \
    expected_version="$(node -p \
      "require('${DSH_PREFIX}/package.json').dependencies['@deepseek-ai/dsh']")"; \
    installed_version="$(node -p \
      "require('${DSH_PREFIX}/node_modules/@deepseek-ai/dsh/package.json').version")"; \
    test "${installed_version}" = "${expected_version}"; \
    npm cache clean --force

# The vendor's web profile must compose in THIS image, not just on a developer
# machine: --dump-config resolves the whole plugin tree and fails loud when the
# npm dependency set cannot produce the profile. The build asserts that it
# composes; it deliberately asserts nothing about WHICH plugins it composes —
# that catalogue is the vendor's product, and a gate over it would make
# AstraBox the curator of someone else's agent.
COPY containers/sandbox-deepseek-harness/astrabox-dsh-profile.patch.yml ${DSH_PREFIX}/profile.patch.yml
RUN set -eux; \
    DSH_HOME=/tmp/dsh-build-home \
      "${DSH_PREFIX}/node_modules/.bin/dsh" --profile web \
      --patch "${DSH_PREFIX}/profile.patch.yml" --dump-config > /dev/null; \
    rm -rf /tmp/dsh-build-home

COPY containers/sandbox-deepseek-harness/astrabox-dsh-serve ${DSH_PREFIX}/serve.sh
COPY containers/sandbox-deepseek-harness/astrabox-dsh-web ${DSH_PREFIX}/web.py
COPY astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror \
     ${DSH_PREFIX}/mirror.py
# The harness writes one append-only log per conversation under its own home,
# and `profile.patch.yml` selects the plaintext encoding so the mirror's batch
# boundary can be a newline rather than the start of the next turn's frame.
ENV ASTRABOX_TRANSCRIPT_MIRROR_ROOT=/home/agent/.dsh/sessions \
    ASTRABOX_TRANSCRIPT_MIRROR_GLOB=*.jsonl \
    ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE=dsh/
COPY containers/sandbox-deepseek-harness/astrabox-dsh-forward ${DSH_PREFIX}/forward.sh
# The shared-box tenancy's per-conversation stack (harness + mirror +
# forwarder), triggered by the platform inside a conversation's isolated
# session rather than supervised at boot — a session's processes die with it.
COPY containers/sandbox-deepseek-harness/astrabox-dsh-serve-conversation \
     ${DSH_PREFIX}/serve-conversation.sh
COPY containers/sandbox-deepseek-harness/supervisord.dsh.conf \
     /opt/gem/supervisord/supervisord.dsh.conf
RUN chmod 0755 "${DSH_PREFIX}/serve.sh" "${DSH_PREFIX}/forward.sh" \
      "${DSH_PREFIX}/serve-conversation.sh"; \
    python3 -m py_compile "${DSH_PREFIX}/web.py"; \
    bash -n "${DSH_PREFIX}/serve.sh"; \
    bash -n "${DSH_PREFIX}/forward.sh"; \
    bash -n "${DSH_PREFIX}/serve-conversation.sh"

# Prove the installed supplier's token exchange and current session/list wire
# before an image can publish a ready TCP port in a real deployment.
# Launcher flags (--patch) precede app flags (--no-open): the first app flag
# ends launcher parsing in the vendor's command grammar.
RUN set -eu; \
    probe_home="$(mktemp -d /tmp/astrabox-dsh-web.XXXXXX)"; \
    HOME="$probe_home" DSH_HOME="$probe_home/.dsh" \
      python3 "${DSH_PREFIX}/web.py" serve \
      "${DSH_PREFIX}/node_modules/.bin/dsh" --profile web \
      --patch "${DSH_PREFIX}/profile.patch.yml" --no-open --host 127.0.0.1 --port 44781 \
      >"$probe_home/boot.log" 2>&1 & \
    vendor_pid=$!; \
    trap 'kill "$vendor_pid" 2>/dev/null || true; wait "$vendor_pid" 2>/dev/null || true; rm -rf "$probe_home"' EXIT; \
    ready=0; \
    for attempt in $(seq 1 120); do \
      if HOME="$probe_home" python3 "${DSH_PREFIX}/web.py" probe 44781; then ready=1; break; fi; \
      kill -0 "$vendor_pid" 2>/dev/null || break; \
      sleep 0.2; \
    done; \
    if [ "$ready" != 1 ]; then cat "$probe_home/boot.log"; exit 1; fi

# The base's workload-account contract, asserted at BUILD time.
#
# This base ships NO account: its entrypoint creates one on every container
# start from `$USER`/`$USER_UID`/`$USER_GID`. Naming that account is the ONLY
# way to get one workload identity, and it is not optional here — the adapter's
# runtime profile declares `SANDBOX_IMAGE_WORKLOAD_USER`, and the platform
# renders that name into every spawn AND into the owner it asks execd to chown
# uploaded files to. An image that leaves the base's default in place looks
# entirely healthy (the CLI runs fine as whoever it is) until a file upload
# fails 500 on an account the box does not have.
#
# Creating a second account here instead would collide: the entrypoint's
# `groupadd --gid 1000` hits a gid this build already took, `set -e` kills it,
# and the container exits 4 before ANY service starts. These checks fail the
# build instead, if a base ever stops honouring the knobs or starts shipping an
# account at the selected uid. Pre-owning the workspace numerically closes the
# race in which a resident service answers before the entrypoint's chown.
RUN set -eux; \
    grep -q 'useradd --uid \$USER_UID' /opt/gem/gem.sh; \
    command -v getent; \
    command -v runuser; \
    ! getent passwd 1000; \
    ! getent group 1000; \
    install -d -o 1000 -g 1000 -m 0755 /workspace

# USER / USER_UID / USER_GID / WORKSPACE are the base image's knobs. Every value
# is a constant of the IMAGE, which is what makes prewarming work: a prepared box
# created before any session exists has run the entrypoint too, so its account
# is identical to a cold-created box's. `astrabox-dsh-serve` reads `$USER` and
# `$WORKSPACE` from here; the host names the same account through
# runtime_profiles.SANDBOX_IMAGE_WORKLOAD_USER, _WORKLOAD_HOME and
# _WORKSPACE_DIR. tests/sandbox_workload_identity_test.py checks that they agree.
# The conversation-assembly script the platform's shared-tenancy bootstrap
# runs in every box (account, home, workspace, caches for one conversation).
# The claude and hermes images always carried it; a shared placement on this
# image died with `astrabox-provision-conversation: No such file or
# directory` until it did too.
COPY astrabox/core/service/orchestrator/runtime/provision-conversation \
     /usr/local/bin/astrabox-provision-conversation
RUN chmod 0555 /usr/local/bin/astrabox-provision-conversation && \
    bash -n /usr/local/bin/astrabox-provision-conversation

# Without this execd loads its default isolation policy, whose writable
# allowlist does not include /home/conversations — every shared placement
# then dies as `create isolated session failed. Status: 400`.
ENV EXECD_ISOLATION_CONFIG=/opt/astrabox/opensandbox/isolation.toml
ENV ASTRABOX_WORKLOAD_USER=agent \
    USER=agent \
    USER_UID=1000 \
    USER_GID=1000 \
    WORKSPACE=/workspace

WORKDIR /workspace
