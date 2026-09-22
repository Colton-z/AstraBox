# syntax=docker/dockerfile:1
#
# AstraBox Community — in-sandbox agent image (image-is-the-contract).
#
# This is the container the sandbox backend starts per session. It bakes the
# Anthropic-official Claude Code CLI at a PINNED version so the image itself is the
# contract: if the CLI is present and runnable, the build succeeds; if it is
# missing or broken, the build fails
# loud. No runtime `npm install` is required on the hot path — the image, not a
# provisioning script, is the unit of compatibility. The agent turn is driven by
# the in-box translation-shell runner (`sandbox_runner.py`, COPY below): it owns
# the Claude Agent SDK session and the host speaks the runner envelope protocol
# over its websocket port. What is genuinely absent is an in-box MODEL proxy —
# the container reaches ANTHROPIC_BASE_URL directly.
#
# Base image: the PUBLIC AIO Sandbox image. The build ARG lets a deployment pin
# a digest or use a compatible fork; it is not a generic code-interpreter seam.
# A replacement must preserve the lifecycle contract described in
# docs/providers/opensandbox.md in addition to passing the build probes below.

# AIO Sandbox (agent-infra/sandbox, Apache-2.0, multi-arch amd64+arm64) remains
# the image lifecycle contract: /opt/gem/run.sh creates the configured workload
# account and stays PID 1 beneath the AstraBox boot wrapper. OpenSandbox file and
# command operations use execd's native APIs, not AIO's :8080 file/shell faces.
# The resident AstraBox runner is the second image-owned process, on :8000.
ARG SANDBOX_BASE_IMAGE=ghcr.io/agent-infra/sandbox:1.11.0

# OpenSandbox's official in-sandbox daemon, pinned like every other image
# dependency. AstraBox treats an isolated terminal as disposable because this
# release cannot reliably cancel and reuse one after its SSE consumer leaves;
# interruption deletes only that terminal session and opens a sibling. Keeping
# the upstream binary untouched makes upgrades and support behavior predictable.
ARG SANDBOX_EXECD_IMAGE=opensandbox/execd:v1.1.0
FROM ${SANDBOX_EXECD_IMAGE} AS execd

FROM ${SANDBOX_BASE_IMAGE}

# OpenSandbox injects execd and bootstrap through every standard lifecycle
# create, including creates issued by the official SDK client-pool creator.
# Prepared boxes therefore use the same /opt/opensandbox daemon as cold boxes;
# they do not bypass injection through an operator-written Pod template.
#
# The pinned image copies below remain alongside the build assertion, but they
# are not the prewarming mechanism. The image-owned isolation policy and
# bubblewrap are still inputs to OpenSandbox isolated sessions. Without bwrap,
# that face answers
# `{"available": false, "message": "bwrap not found"}` — honestly, but the
# capability is simply absent, and a sandbox that cannot start an isolated
# session has to give a whole container to every task that must not see its
# siblings. The daemon looks for it on $PATH and under /opt/opensandbox.
#
# Baking it does NOT make the capability work: what refuses the nested namespace
# is outside the image (Docker's default seccomp profile; on Kubernetes the
# host's apparmor_restrict_unprivileged_userns). See docs/providers/opensandbox.md.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends bubblewrap; \
    rm -rf /var/lib/apt/lists/*; \
    bwrap --version

COPY --from=execd /execd /execd
COPY --from=execd /bootstrap.sh /bootstrap.sh
COPY containers/sandbox-claude-code/execd-isolation.toml /opt/astrabox/opensandbox/isolation.toml
RUN set -eux; \
    mkdir -p /opt/astrabox/opensandbox; \
    ln /execd /opt/astrabox/opensandbox/execd; \
    ln /bootstrap.sh /opt/astrabox/opensandbox/bootstrap.sh; \
    chmod 0555 /execd /bootstrap.sh; \
    chmod 0444 /opt/astrabox/opensandbox/isolation.toml; \
    /opt/astrabox/opensandbox/execd --help 2>&1 | grep -q 'OpenSandbox Execd'

# Pinned Claude Code CLI version — the build-time contract.
# Override at build with `--build-arg CLAUDE_CODE_VERSION=<x.y.z>`. Default
# tracks the current published `@anthropic-ai/claude-code` release on the public
# npm registry (verified via `npm view @anthropic-ai/claude-code version`); bump
# deliberately, never float to a moving tag, so the image digest pins the exact
# agent the host SDK speaks to.
ARG CLAUDE_CODE_VERSION=2.1.266

# Public npm registry only (the default registry.npmjs.org below). Overridable for
# air-gapped/private mirrors.
ARG NPM_REGISTRY=https://registry.npmjs.org/

ARG DEBIAN_FRONTEND=noninteractive

# Some public code-interpreter bases run as a non-root user by default; a global
# `npm install -g` needs write access to the global prefix, so install as root
# and (if the base defined one) drop back afterward. Most code-interpreter bases
# ship Node.js already; if a chosen base does not, add it in a fork of this file
# — Community does not assume a specific package manager here.
USER root

# The base's workload-account contract, asserted at BUILD time.
#
# This base ships NO account: its entrypoint creates one on every container
# start, from `$USER` / `$USER_UID` / `$USER_GID`, and runs the :8080 services as
# it (`gem.sh`: `groupadd --gid $USER_GID $USER`; `supervisord.python_srv.conf`:
# `user=%(ENV_USER)s --workspace %(ENV_WORKSPACE)s`). Naming that account is
# therefore the ONLY way to get one workload identity — the ENV block at the end
# of this file does it.
#
# Creating a second account here instead would collide: the entrypoint's
# `groupadd --gid 1000` hits a gid this build already took, `set -e` kills it,
# and the container exits 4 before ANY service starts — not just the agent, the
# whole box. These checks fail the build instead, if a base ever stops honouring
# the knobs or starts shipping an account at the selected uid. Numeric ownership
# does not register an account or group; pre-owning only the workspace closes the
# race in which the image-owned runner answers before the entrypoint's chown.
RUN set -eux; \
    grep -q 'ENV_USER' /opt/gem/supervisord/supervisord.python_srv.conf; \
    grep -q 'ENV_WORKSPACE' /opt/gem/supervisord/supervisord.python_srv.conf; \
    grep -q 'useradd --uid \$USER_UID' /opt/gem/gem.sh; \
    command -v getent; \
    command -v runuser; \
    ! getent passwd 1000; \
    ! getent group 1000; \
    install -d -o 1000 -g 1000 -m 0755 /workspace

# git is required inside the box: the conversation bootstrap clones the default
# repo and the skill-cache prep clones each declared skill (git-descriptor install).
# Install it explicitly and PROVE it runs — image-is-the-contract, fail the build
# loud if absent (no runtime apt-get on the hot path, no silent skip of a clone).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git; \
    rm -rf /var/lib/apt/lists/*; \
    git --version

# OpenSandbox command transport triggers this fixed platform capability when a
# session claims the box. It must already exist in a prewarmed image; the claim
# path supplies only the conversation-specific environment and data.
COPY astrabox/core/service/orchestrator/runtime/provision-conversation /usr/local/bin/astrabox-provision-conversation
RUN chmod 0555 /usr/local/bin/astrabox-provision-conversation && \
    bash -n /usr/local/bin/astrabox-provision-conversation

COPY astrabox/core/service/orchestrator/runtime/astrabox-assistant-workspace-storage /usr/local/bin/astrabox-assistant-workspace-storage
RUN chmod 0555 /usr/local/bin/astrabox-assistant-workspace-storage && \
    bash -n /usr/local/bin/astrabox-assistant-workspace-storage

# Install the pinned CLI globally, then PROVE it is runnable.
#   * `npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}` — bakes the CLI.
#   * `command -v claude` — the binary is on PATH.
#   * `claude --version` — the binary actually executes (the image-is-the-contract
#     smoke test). If the CLI is broken, ABI-mismatched, or the version
#     was yanked, THIS LINE FAILS THE BUILD — no silent fallback, no
#     install-at-runtime path (fail loud, single correct path). The printed
#     version is also captured in the build log as the image's recorded contract
#     version.
RUN set -eux; \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" --registry="${NPM_REGISTRY}"; \
    command -v claude; \
    claude --version

# Make the bare `claude` (and the node it may spawn) resolve on the agent
# transport's PATH. The host's docker-exec transport runs `claude` under a PATH
# that contains /usr/local/bin but NOT this AIO base's node prefix
# (/opt/nodejs/<v>/bin, where npm -g put claude + node) — so a bare `claude`
# exec returns 127 (command not found) and the agent dies on connect. Symlink
# both into /usr/local/bin, then PROVE it under a transport-style PATH that has
# no /opt/nodejs entry: if the bare `claude` can't run there, THIS LINE FAILS
# THE BUILD (image-is-the-contract; no runtime PATH patching, no fallback).
RUN set -eux; \
    ln -sf "$(command -v claude)" /usr/local/bin/claude; \
    ln -sf "$(command -v node)" /usr/local/bin/node; \
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        /usr/local/bin/claude --version

# In-box runner runtime. The runner owns the long-lived Claude Agent SDK session
# (and through it the `claude` CLI), so interrupt / interaction answers reach a
# live process; the host connects to its websocket on :8000 (published at
# container create). It coexists with the AIO base lifecycle started below.
#
# AIO 1.11.0 ships its own Python 3.12 beside Ubuntu's default Python 3.10.
# The runner uses that supplier-provided 3.12 face explicitly: websockets 17.1
# requires Python >=3.11, while a bare `pip3` resolves against 3.10 and cannot
# install it. Do not repoint the base's global `python3`; AIO owns the services
# already using that interpreter. Install and launch this one image capability
# with /usr/local/bin/python3.12 throughout. No --break-system-packages is needed
# because the dedicated interpreter has no PEP 668 "EXTERNALLY-MANAGED" marker.
# The import smoke test fails the build loud if any dependency is missing or
# broken (image-is-the-contract; no runtime pip on the hot path).
ARG CLAUDE_AGENT_SDK_VERSION=0.2.152
ARG WEBSOCKETS_VERSION=17.1
# The SDK accepts both MCP major versions; pin the complete server/client API
# used by in-box tools instead of inheriting the base image's satisfied version.
ARG MCP_VERSION=2.1.1
RUN set -eux; \
    /usr/local/bin/pip3.12 install --no-cache-dir \
        "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" \
        "websockets==${WEBSOCKETS_VERSION}" \
        "mcp==${MCP_VERSION}" \
        anyio; \
    /usr/local/bin/python3.12 -c "import claude_agent_sdk, websockets, anyio; print('inbox deps ok', claude_agent_sdk.__version__, websockets.__version__)"; \
    /usr/local/bin/python3.12 -c "from importlib.metadata import version; from mcp import Client; from mcp.server import MCPServer; assert version('mcp') == '${MCP_VERSION}'; print('inbox MCP APIs ok', version('mcp'), Client.__name__, MCPServer.__name__)"; \
    /usr/local/bin/python3.12 -c "import subprocess; actual = subprocess.check_output(['/usr/local/bin/claude', '--version'], text=True).split()[0]; assert actual == '${CLAUDE_CODE_VERSION}', f'image CLI {actual} differs from pinned ${CLAUDE_CODE_VERSION}'"

# Bake the in-box runner (docs/design-translation-shell-2026-07.md). Started by
# THIS IMAGE's own boot script (boot.sh below), not by the host reaching into
# the box: a prepared box can start before its Session exists, and a resumed box
# starts new processes. The host executor's launch stays as an idempotent
# fallback (it
# adopts a listening runner). NOT PID 1 — the AIO base's own entrypoint stays
# the contract and keeps the :8080 server alive. Self-contained: stdlib +
# claude_agent_sdk only. Build context is the repo ROOT (e.g.
# `docker build -f containers/sandbox-claude-code/Dockerfile .`).
COPY astrabox/core/service/orchestrator/sandbox_runner.py /opt/astrabox/sandbox_runner.py
RUN /usr/local/bin/python3.12 -c "import ast; ast.parse(open('/opt/astrabox/sandbox_runner.py').read()); print('runner parse ok')"

# The image and the host revive path invoke the same launcher. It waits for the
# AIO base to create the fixed account, then drops the whole runner process to
# that account before the SDK can spawn a CLI child.
COPY containers/sandbox-claude-code/start-runner.sh /opt/astrabox/start-runner.sh
RUN chmod 0755 /opt/astrabox/start-runner.sh && sh -n /opt/astrabox/start-runner.sh

# The boot contract: start the runner, then exec the base's init. This is the
# image's ENTRYPOINT as far as the platform is concerned — the create path
# passes it explicitly (`sandbox_image.AIO_IMAGE_ENTRYPOINT`), so the two move
# together.
COPY containers/sandbox-claude-code/boot.sh /opt/astrabox/boot.sh
RUN chmod 0755 /opt/astrabox/boot.sh && sh -n /opt/astrabox/boot.sh


# Agent-runtime environment contract. The host executor injects the per-session
# values (ANTHROPIC_BASE_URL / _AUTH_TOKEN / _MODEL) at sandbox-create time; the
# two static knobs below are baked because they are invariant for a sandboxed
# agent:
#   * IS_SANDBOX=1 — tells the CLI it runs inside an isolated sandbox.
#   * CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 — suppress non-essential
#     outbound calls (telemetry/update checks) from inside the box.
# There is NO in-box model proxy: the container reaches ANTHROPIC_BASE_URL
# directly over its normal egress.
#
# USER / USER_UID / USER_GID / WORKSPACE are the base image's knobs (see the
# build-time assertion above). They configure one fixed workload identity for
# the entrypoint, the :8080 file API, the terminal, code-server, and the agent
# process. Sharing that identity keeps mounted-workspace ownership consistent
# across every service in the sandbox.
#
# Every value is a constant of the IMAGE, which is what makes prewarming work:
# the entrypoint runs when the container starts, so a prepared box created before
# any session exists has run it too, and its account is identical to a
# cold-created box's. The spawn names this account through
# runtime_profile.SANDBOX_IMAGE_WORKLOAD_USER, _WORKLOAD_HOME, and
# _WORKSPACE_DIR; those values must agree with the image or the CLI fails with
# `getpwnam(): name not found`. tests/sandbox_workload_identity_test.py checks
# that relationship.
#
# The uid is the same in every sandbox, deliberately: sandboxes are isolated by
# what is MOUNTED into them, not by which number owns files inside them — see
# docs/providers/opensandbox.md, "Isolation is what you mount".
ENV IS_SANDBOX=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    EXECD_ISOLATION_CONFIG=/opt/astrabox/opensandbox/isolation.toml \
    ASTRABOX_WORKLOAD_USER=agent \
    USER=agent \
    USER_UID=1000 \
    USER_GID=1000 \
    WORKSPACE=/workspace

# The image layer owns this path by the fixed numeric uid before either startup
# process can answer, and the entrypoint creates the matching named account. The
# CLI is spawned into it. A deployment that mounts durable storage over the path
# owns making that mount writable by uid 1000, which is why the uid is fixed.
WORKDIR /workspace
