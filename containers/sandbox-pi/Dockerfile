# syntax=docker/dockerfile:1
#
# AstraBox — in-sandbox pi image (image-is-the-contract).
#
# The container the sandbox backend starts for a `pi` session. It bakes the
# vendor's CLI (`@earendil-works/pi-coding-agent`, open source) at a PINNED
# version, so the image itself is the contract: if pi is present, runnable and
# emits the event this adapter's turn terminal depends on, the build succeeds;
# otherwise it fails loud. AstraBox composes nothing beyond that — what pi IS
# (its tools, its prompt, its session format) is the vendor's product decision.
#
# Unlike the harness image there is no supervised engine. Pi's headless surface
# is a process that owns one conversation on its stdin/stdout, so the adapter
# starts `pi --mode rpc` per conversation through execd's PTY pipe. This image
# provides the capability, not the process: the CLI, the account it runs as, and
# the provider configuration it reads, all present before any session claims the
# box — which is what keeps a prewarmed box valid.
#
# The transcript mirror outlives individual Pi processes so their session
# files remain recoverable. Shared conversations supervise it together with
# an execd daemon inside the conversation's existing isolation namespace.
#
# Build (repo-root context):
#   docker build -t astrabox/sandbox-pi:latest \
#     -f containers/sandbox-pi/Dockerfile .
ARG SANDBOX_BASE_IMAGE=ghcr.io/agent-infra/sandbox:1.11.0

# OpenSandbox's official in-sandbox daemon, pinned like every other image
# dependency. OpenSandbox injects it through every standard lifecycle create,
# including creates issued by the official SDK client-pool creator. The pinned
# image copy also serves each shared conversation from inside its existing
# isolation session. Both placements use execd's supported PTY pipe.
ARG SANDBOX_EXECD_IMAGE=opensandbox/execd:v1.1.0
FROM ${SANDBOX_EXECD_IMAGE} AS execd

FROM ${SANDBOX_BASE_IMAGE}

# Pinned CLI version — the build-time contract with the vendor.
#
# The release's package.json requires Node >=22.19.0. Pin both the package
# and its Node floor so npm cannot silently select a different release line.
# The supported embedding surface remains the SDK and stdio RPC; the
# experimental client/server source exports are not a replacement transport.
ARG PI_VERSION=0.85.1
ARG PI_NODE_MINIMUM=22.19.0
# Pi ships no sub-agents and says so: "It intentionally does not include
# built-in MCP, sub-agents, permission popups, plan mode, to-dos, or background
# bash. You can build or install those workflows as extensions or packages."
# This package is that install. It was chosen over `@everyx/pi-subagent` and
# `@tintinweb/pi-subagents`, which give the same background children and the
# same per-child stop, for one reason the platform cannot do without: it is the
# only one whose stop is reachable by anything but the model. Theirs are tools
# the model calls; this one also registers `/subagents-stop <run-id>
# <child-id>`, and pi's RPC executes an extension command sent as a prompt even
# mid-stream. `SingleResult.index` paired with `Details.runId` is the vendor's
# own stable child identity, which is what the platform projects.
ARG PI_SUBAGENTS_VERSION=0.66.0
ARG PI_PREFIX=/opt/pi
ARG NPM_REGISTRY=https://registry.npmjs.org/
ARG DEBIAN_FRONTEND=noninteractive

USER root

COPY --from=execd /execd /execd
COPY --from=execd /bootstrap.sh /bootstrap.sh
# The isolation half of shared tenancy: the platform places every
# conversation in an execd isolation session, which needs bubblewrap and the
# shared policy (its allowlist names /home/conversations).
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

# The base's workload-account contract, asserted at BUILD time — the same
# contract the Claude image asserts, for the same reason. This base ships NO
# account: its entrypoint creates one on every container start from `$USER` /
# `$USER_UID` / `$USER_GID`, so naming that account in the ENV block below is
# the only way to get one. Creating a second account here would collide with
# that entrypoint and kill the whole box before any service starts.
RUN set -eux; \
    grep -q 'ENV_USER' /opt/gem/supervisord/supervisord.python_srv.conf; \
    grep -q 'useradd --uid \$USER_UID' /opt/gem/gem.sh; \
    command -v getent; \
    command -v runuser; \
    ! getent passwd 1000; \
    ! getent group 1000; \
    install -d -o 1000 -g 1000 -m 0755 /workspace

# jq renders models.json at boot, so a base URL or model id containing a quote
# cannot produce a broken file. Installed rather than assumed: the renderer
# runs on the box's boot path, and a missing tool there fails every start.
RUN set -eux; \
    if ! command -v jq >/dev/null; then \
      apt-get update; \
      apt-get install -y --no-install-recommends jq; \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    jq --version

# The Node floor pi's own package declares. Asserted here so a base-image bump
# that moves Node backwards fails the build instead of silently downgrading the
# engine to a line whose turns never end.
RUN set -eux; \
    node_version="$(node --version | sed 's/^v//')"; \
    lowest="$(printf '%s\n%s\n' "${node_version}" "${PI_NODE_MINIMUM}" | sort -V | head -n1)"; \
    test "${lowest}" = "${PI_NODE_MINIMUM}" \
      || { echo "base image ships Node ${node_version}; pi ${PI_VERSION} requires >= ${PI_NODE_MINIMUM}. npm would silently install the legacy-node20 line, which never emits agent_settled and whose turns never end." >&2; exit 1; }

# Installed under a prefix AstraBox owns rather than the base image's global
# node prefix, so the CLI's dependency set cannot shadow a base-image tool the
# sandbox's own services resolve from PATH.
RUN set -eux; \
    mkdir -p "${PI_PREFIX}"; \
    npm install --prefix "${PI_PREFIX}" --no-audit --no-fund \
      --registry="${NPM_REGISTRY}" \
      "@earendil-works/pi-coding-agent@${PI_VERSION}"; \
    npm cache clean --force

# Prove the pin took. `npm install <pkg>@<version>` can still land elsewhere if
# the range is unsatisfiable on this Node, and that failure mode is silent, so
# the version is read back from what was actually installed.
RUN set -eux; \
    installed="$(node -p "require('${PI_PREFIX}/node_modules/@earendil-works/pi-coding-agent/package.json').version")"; \
    test "${installed}" = "${PI_VERSION}" \
      || { echo "requested pi ${PI_VERSION} but ${installed} is installed" >&2; exit 1; }

# The sub-agent package, installed at build under the same owned prefix and
# read back the same way. Fetching it here rather than through `pi install`
# keeps the box off the network at conversation time and makes the version an
# image fact instead of whatever the registry served that morning.
RUN set -eux; \
    npm install --prefix "${PI_PREFIX}" --no-audit --no-fund \
      --registry="${NPM_REGISTRY}" \
      "pi-subagents@${PI_SUBAGENTS_VERSION}"; \
    npm cache clean --force; \
    installed="$(node -p "require('${PI_PREFIX}/node_modules/pi-subagents/package.json').version")"; \
    test "${installed}" = "${PI_SUBAGENTS_VERSION}" \
      || { echo "requested pi-subagents ${PI_SUBAGENTS_VERSION} but ${installed} is installed" >&2; exit 1; }

# The one command the platform's child-run stop rides. Asserted at build time
# for the reason the `agent_settled` assertion below is: without it every
# conversation still starts and every sub-agent still runs, and only a stop
# request finds out.
RUN set -eux; \
    grep -rq 'subagents-stop' "${PI_PREFIX}/node_modules/pi-subagents/src" \
      || { echo "pi-subagents ${PI_SUBAGENTS_VERSION} registers no subagents-stop command; the platform cannot stop a child run" >&2; exit 1; }

# The one vendor event the adapter's turn terminal depends on. Asserted at
# build time, where it costs one image build, instead of in a conversation that
# streams a complete answer and then never settles.
RUN set -eux; \
    grep -rq 'agent_settled' "${PI_PREFIX}/node_modules/@earendil-works/pi-coding-agent/dist" \
      || { echo "pi ${PI_VERSION} does not emit agent_settled; this adapter's turns would never terminate" >&2; exit 1; }

# Make the bare `pi` — and the node its shebang resolves — reachable on the
# transport's PATH. The adapter starts pi through execd, whose PATH contains
# /usr/local/bin but NOT this base's node prefix (/opt/nodejs/<v>/bin). Without
# both symlinks a bare `pi` exits 127 and the turn dies on connect. Proven
# under a transport-style PATH that has no /opt/nodejs entry, so the build
# fails here rather than the first conversation.
RUN set -eux; \
    ln -sf "${PI_PREFIX}/node_modules/.bin/pi" /usr/local/bin/pi; \
    ln -sf "$(command -v node)" /usr/local/bin/node; \
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        /usr/local/bin/pi --version

# Every model call leaves this box through the egress sidecar, which terminates
# TLS to substitute the real credential and therefore presents its own
# certificate. It installs the issuing CA into the OS trust store, which is
# enough for OpenSSL and for anything statically linked against the system
# roots; Node builds its own list and ignores that store, so without this the
# handshake fails with UNABLE_TO_VERIFY_LEAF_SIGNATURE. Pi reports it as
# "Connection error." with no status code, retries three times and settles the
# turn empty, so the failure reads as an unreachable gateway rather than an
# untrusted one — measured in a real box, where the same request from OpenSSL
# on the same account returned 200.
#
# This names the merged bundle rather than the injected file: the sidecar
# writes its CA under /usr/local/share/ca-certificates and the box's own
# startup folds it in, so a box with no interception still has a valid bundle
# here and nothing to special-case.
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# A base image that moves or drops the merged bundle leaves that variable
# pointing at nothing, and Node says nothing about it: an unreadable path is
# ignored silently, without a warning and without a non-zero exit — measured on
# this image's own Node. The setting would then be inert and the first model
# call of the first conversation would fail the handshake instead. Fail here,
# where the cause is still legible.
RUN set -eux; \
    test -s "${NODE_EXTRA_CA_CERTS}" \
      || { echo "no system CA bundle at ${NODE_EXTRA_CA_CERTS}; Node cannot verify the egress sidecar's certificate" >&2; exit 1; }

# Pi's configuration directory, pre-owned by the fixed numeric uid before the
# entrypoint registers the matching named account. Numeric ownership is what
# lets the image prepare it without creating an account: pi writes its model
# store and session files here as that account.
RUN set -eux; \
    install -d -o 1000 -g 1000 -m 0755 /home/agent /home/agent/.pi \
        /home/agent/.pi/agent /home/agent/.pi/sessions

# The renderer writes pi's provider configuration once the base image has
# created the account. It is backgrounded by boot.sh below.
COPY containers/sandbox-pi/render-models-config.sh /opt/astrabox/render-models-config.sh
COPY containers/sandbox-pi/boot.sh /opt/astrabox/boot.sh

# The transcript mirror. Pi writes each conversation to one append-only JSONL
# file under its session directory and hands none of it over the RPC surface, so
# the box is the only place that conversation exists: a reclaimed box takes it
# along and the next one exits with `No session found matching '<id>'`. One
# program serves every image with that shape, installed from `runtime/` where
# image-owned programs live and told by the environment below which files this
# engine writes.
#
# These three values are the other side of the adapter's own declaration
# (`PiEngineAdapter.capabilities.session_log`). They are the same two paths and
# one prefix written in two files, and a rename on either side would mirror the
# wrong tree — or none — while every box still came up healthy and every turn
# still streamed. `tests/transcript_mirror_seam_test.py` holds them together.
COPY astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror \
     ${PI_PREFIX}/mirror.py
ENV ASTRABOX_TRANSCRIPT_MIRROR_ROOT=/home/agent/.pi/sessions \
    ASTRABOX_TRANSCRIPT_MIRROR_GLOB=*.jsonl \
    ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE=pi/
# The platform starts this authenticated execd and mirror stack inside the
# existing conversation isolation session. Pi's PTY child inherits that
# session's /workspace bind and account; all its processes die with it.
COPY containers/sandbox-pi/astrabox-pi-serve-conversation \
     ${PI_PREFIX}/serve-conversation.sh
COPY containers/sandbox-pi/supervisord.pi.conf \
     /opt/gem/supervisord/supervisord.pi.conf

# The interpreter the relay runs under, and that the file it will run parses.
# Asserted at build time because both failures are silent at run time: a missing
# python3 leaves the program in FATAL and a syntax error leaves it crash-looping,
# while the box reports READY, every turn streams a complete answer, and the
# conversation is simply never written down.
RUN set -eux; \
    python3 -c "import hashlib, json, urllib.request"; \
    python3 -m py_compile "${PI_PREFIX}/mirror.py"; \
    chmod 0755 "${PI_PREFIX}/serve-conversation.sh"; \
    bash -n "${PI_PREFIX}/serve-conversation.sh"; \
    rm -rf "${PI_PREFIX}/__pycache__"
RUN set -eux; \
    chmod 0755 /opt/astrabox/render-models-config.sh /opt/astrabox/boot.sh; \
    sh -n /opt/astrabox/render-models-config.sh; \
    sh -n /opt/astrabox/boot.sh

# The base image's knobs, which configure the ONE workload identity its
# entrypoint creates and every in-box service runs as. The adapter's runtime
# profile names the same account, home and workspace; they must agree or pi
# fails with `getpwnam(): name not found`.
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
    WORKSPACE=/workspace \
    PI_CODING_AGENT_DIR=/home/agent/.pi/agent

# The boot contract: render pi's provider configuration, then become the base's
# init. The create path passes this entrypoint explicitly (pi.py's
# PI_IMAGE_ENTRYPOINT), so the two move together.
ENTRYPOINT ["/opt/astrabox/boot.sh"]
WORKDIR /workspace
