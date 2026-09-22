# syntax=docker/dockerfile:1
#
# AstraBox Community — backend SERVER image.
#
# The one-line-`docker run` target.
# Packages the `astrabox` wheel and runs the `astrabox serve` CLI (uvicorn over
# the FastAPI app from astrabox.api.app:create_app) on :8000. Community builds on
# the public `python:3.12-slim` base only (matches pyproject
# `requires-python = ">=3.12"`).
#
# IMPORTANT layout note: this repo is a FLAT layout. The import package lives at
# `./astrabox` (there is NO `src/` directory), and pyproject declares
# `[tool.hatch.build.targets.wheel] packages = ["astrabox"]`. The wheel build
# below copies `pyproject.toml` + `./astrabox` accordingly — copying a `src/`
# tree would fail because none exists.
#
# Multi-stage:
#   * stage `web`     — builds the console SPA (Vite) into frontend/dist so the
#                       server serves the UI directly (`:8000` opens the console).
#   * stage `channel` — installs the exact Node adapter graph used by concrete
#                       chat providers; it stays separate from Python deps.
#   * stage `build`   — PEP 517 front-end builds the wheel via the hatchling
#                       backend declared in pyproject [build-system].
#   * stage `dns`     — supplies the pinned CoreDNS binary used only for the
#                       private embedded-gateway name.
#   * stage `runtime` — installs ONLY the built wheel + its resolved deps into a
#                       clean slim base, copies the built SPA, creates a non-root
#                       user, runs the CLI.
#
# PID 1 is `tini` (baked in, not `docker run --init`), because this image may run
# up to FIVE processes (AstraBox, the lifecycle server, the embedded model
# gateway, its private DNS resolver, and the channel adapter gateway): see the
# entry point at the bottom. uvicorn handles SIGTERM for
# graceful shutdown; the app's lifespan hook then runs the existing service
# cleanup (close_services_for_lifecycle). `--init` / compose `init: true` remains
# harmless — tini tolerates being run under another init.
#
# Docker socket note: on the default ASTRABOX_SANDBOX_BACKEND=open_sandbox this
# image also starts the OpenSandbox lifecycle server, and THAT is what talks to
# the host Docker daemon over a bind-mounted /var/run/docker.sock. The socket is
# provided at `docker run` time (see compose.yaml), not baked into the image. The
# in-sandbox agent image is built separately (containers/sandbox-claude-code/Dockerfile);
# astrabox/config/release_images.py names the image a deployment runs by default.
#
# ONE CONTAINER, EITHER SHAPE. The image also carries the OpenSandbox lifecycle
# server (the `sandbox-server` extra), so `-e ASTRABOX_SANDBOX_BACKEND=open_sandbox`
# is the whole opt-in to that backend: the entry point starts the lifecycle server
# on container loopback, waits for it, and runs AstraBox in front of it. `docker ps`
# still shows one container and the only published port is still AstraBox's. Set
# ASTRABOX_SANDBOX_OPENAPI_BASE_URL instead to use a lifecycle server you run
# yourself, and nothing extra starts here. See astrabox/deploy/onebox.py.

# ---------------------------------------------------------------------------
# Stage 0: build the console SPA
# ---------------------------------------------------------------------------
ARG NODE_VERSION=26.8.1
FROM node:${NODE_VERSION}-slim AS web

WORKDIR /web

# Install deps first (cache on the lockfile), then build. `npm run build` runs
# `tsc --noEmit && vite build` (the real typecheck gate) → ./dist.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 1: install the channel adapter runtime
# ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-slim AS channel

WORKDIR /gateway

# The local file-type compatibility package is part of the locked dependency
# graph, so it must be present before npm ci resolves file:vendor/.... Adapter
# install scripts are unnecessary; disabling them keeps the third-party build
# boundary deterministic.
COPY channel-gateway/package.json channel-gateway/package-lock.json ./
COPY channel-gateway/vendor/ ./vendor/
RUN npm ci --ignore-scripts
COPY channel-gateway/src/ ./src/

# ---------------------------------------------------------------------------
# Stage 2: build the wheel
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# `build` is the PEP 517 front-end; it drives the hatchling backend from
# pyproject [build-system]. The wheel's own runtime deps are resolved later from
# the wheel metadata, so pin nothing else here.
RUN pip install "build==1.6.0"

# Copy packaging inputs first for layer caching, then the FLAT package tree.
# hatchling reads [tool.hatch.build.targets.wheel] packages = ["astrabox"] — the
# package is the top-level ./astrabox directory (no src/ layout).
COPY pyproject.toml README.md LICENSE ./
COPY astrabox/ ./astrabox/

# Produce a wheel (not an sdist) into /dist.
RUN python -m build --wheel --outdir /dist .

# ---------------------------------------------------------------------------
# Stage 3: private DNS binary for the embedded model gateway
# ---------------------------------------------------------------------------
FROM coredns/coredns:1.13.1 AS dns

# ---------------------------------------------------------------------------
# Stage 4: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# - PYTHONUNBUFFERED: stream logs immediately so `docker logs` is live.
# - ASTRABOX_* bake the Community design defaults:
#   open_sandbox sandbox backend and PostgreSQL persistence.
#   All overridable at `docker run` time via -e / --env-file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ASTRABOX_HOST=0.0.0.0 \
    ASTRABOX_PORT=8000 \
    ASTRABOX_SANDBOX_BACKEND=open_sandbox \
    ASTRABOX_STATE_DIR=/data \
    ASTRABOX_DB_BACKEND=postgresql \
    ASTRABOX_FRONTEND_DIST=/app/frontend/dist

# NOTE ON ASTRABOX_HOST=0.0.0.0: the server binds all interfaces INSIDE its own
# network namespace — that is safe because the container is isolated and the
# host-side publish is what controls exposure. Publish to loopback
# (`-p 127.0.0.1:8088:8000`, the documented default) so the no-auth admin
# surface is not reachable from the LAN.

# tini is PID 1 (see the header). apt rather than a release download so the
# package manager owns the signature check, and the lists are dropped in the same
# layer to keep the image lean. Prisma's build-time Node binary links libatomic;
# keep it in the runtime image because LiteLLM also runs schema migrations when
# it starts. The bundled Node binary needs libstdc++ at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# ── the embedded LiteLLM gateway ────────────────────────────────────────────
# The default model gateway lives IN this image, in its OWN venv — never the
# app's: the proxy's dependency tree is large and moves weekly, and sharing a
# resolver with the platform's pins would make every LiteLLM bump a platform
# dependency event. The entry point (astrabox/deploy/onebox.py) starts it as a
# second supervised process unless ASTRABOX_LITELLM_BASE_URL points at a
# gateway the operator runs. Pin + bump deliberately; scripts/dev.sh mirrors
# this version for source-dev (bump both together).
# `proxy` installs the HTTP gateway; `extra-proxy` installs the Prisma client
# and the other database-backed control-plane dependencies. The latter is
# required as soon as DATABASE_URL is set (the Compose default). Installing
# only `proxy` boots without persistence and then crashes with
# ModuleNotFoundError: prisma when PostgreSQL is enabled.
# The bundled config registers Langfuse callbacks even when credentials are
# absent (the client then self-disables), so its pinned SDK must exist. Install
# that one integration explicitly instead of LiteLLM's much larger
# `proxy-runtime` extra, which also pulls cloud SDKs AstraBox does not use.
#
# The Python package ships its schema but not a generated Prisma client. Generate
# that client while the image still has network access. The dummy URL is parsed
# but never contacted by `generate`; the real DATABASE_URL arrives at runtime,
# and schema migrations remain LiteLLM's responsibility.
#
# Generation runs as root, but the gateway runs as `astrabox`. Keep both the
# generated engine and cached Prisma CLI outside /root, readable by the runtime
# account. The CLI symlink lets LiteLLM's migration subprocess find `prisma`
# without putting the LiteLLM venv first in the main process's PATH (which would
# also replace AstraBox's Python interpreter).
ARG LITELLM_VERSION=1.99.0
ARG LANGFUSE_VERSION=4.15.1
ENV PRISMA_BINARY_CACHE_DIR=/opt/litellm/prisma-binaries \
    PRISMA_NODEENV_CACHE_DIR=/opt/litellm/prisma-nodeenv
RUN python -m venv /opt/litellm \
    && /opt/litellm/bin/pip install --no-cache-dir \
       "litellm[proxy,extra-proxy]==${LITELLM_VERSION}" \
       "langfuse==${LANGFUSE_VERSION}" \
    && PATH="/opt/litellm/bin:${PATH}" \
       DATABASE_URL=postgresql://unused:unused@127.0.0.1:5432/unused \
       /opt/litellm/bin/prisma generate \
       --schema=/opt/litellm/lib/python3.12/site-packages/litellm/proxy/schema.prisma \
    && chmod -R a+rX "$PRISMA_BINARY_CACHE_DIR" "$PRISMA_NODEENV_CACHE_DIR" \
    && ln -s /opt/litellm/bin/prisma /usr/local/bin/prisma

# Install the wheel + its resolved dependencies from the build stage. Keep this
# application-dependent layer after the independently pinned LiteLLM venv: an
# AstraBox source edit rebuilds the wheel, but must not download and install the
# gateway's much larger dependency tree again.
#
# Installing by glob lets pip pull the runtime deps from the wheel METADATA
# (fastapi, uvicorn[standard], claude-agent-sdk, aiodocker, sqlalchemy, mcp, …).
# No compiler toolchain is needed — the core deps ship manylinux/glibc wheels.
# `[sandbox-server]` adds the OpenSandbox lifecycle server (and its docker /
# kubernetes / redis tail) so the open_sandbox backend needs no second image.
# `[aws-kms]` keeps the published image ready for stateless cloud deployments;
# source/library installs remain free of the AWS SDK unless they select it.
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    pip install "$(echo /dist/*.whl)[aws-kms,sandbox-server]"

# The built console SPA, served by the app at ASTRABOX_FRONTEND_DIST so the
# published port opens the UI (not raw JSON).
COPY --from=web /web/dist /app/frontend/dist

# ── concrete chat-channel adapters ─────────────────────────────────────────
# Satori is an internal protocol/runtime detail. The user configures Telegram,
# Discord, Feishu, and the other concrete providers through AstraBox; onebox
# starts this private process unless an external gateway URL is configured.
# Copy the exact Node executable from the same pinned stage that resolved the
# lockfile, then the lockfile-exact runtime tree and shared provider manifest.
COPY --from=channel /usr/local/bin/node /opt/astrabox/channel-gateway/bin/node
COPY --from=channel /gateway /opt/astrabox/channel-gateway
COPY astrabox/providers/channel_gateway_manifest.json /opt/astrabox/channel-gateway/channel_gateway_manifest.json
ENV ASTRABOX_CHANNEL_GATEWAY_MANIFEST=/opt/astrabox/channel-gateway/channel_gateway_manifest.json

# The routing table (model_name → upstream provider). Baked so a bare
# `docker run` has multi-provider routing; mount over this path to edit it
# without a rebuild.
COPY containers/litellm/config.yaml /opt/astrabox/litellm/config.yaml
# LiteLLM stays in its own venv. Copy only the provider-neutral OIDC primitive
# and the thin gateway adapter beside its config; the platform package and its
# dependency graph are not installed into that venv.
COPY astrabox/identity/oidc.py /opt/astrabox/litellm/astrabox_oidc.py
COPY astrabox/identity/session_signing.py /opt/astrabox/litellm/astrabox_identity_session.py
COPY astrabox/providers/litellm_shared_auth.py /opt/astrabox/litellm/astrabox_litellm_auth.py
COPY containers/litellm/custom_auth.py /opt/astrabox/litellm/custom_auth.py
COPY containers/litellm/langfuse_session_hook.py /opt/astrabox/litellm/langfuse_session_hook.py
COPY containers/litellm/stream_normalization_hook.py /opt/astrabox/litellm/stream_normalization_hook.py

# Credential Vault bindings require an FQDN. The private RFC 2606 `.test` name
# is resolved inside the deployment, never through public DNS; onebox starts
# this binary only for the protected embedded-gateway shape.
COPY --from=dns /coredns /opt/coredns/coredns
COPY containers/coredns/Corefile /opt/astrabox/coredns/Corefile

# Non-root runtime user. The bundled lifecycle server reaches Docker via the
# bind-mounted socket; socket access is granted at run time (e.g.
# `--group-add $(stat -c %g /var/run/docker.sock)`), NOT by running as root.
# /data is owned by the app user so local workspaces and generated keys are writable.
RUN useradd --system --create-home --home-dir /home/astrabox --shell /usr/sbin/nologin astrabox \
    && mkdir -p /data \
    && chown -R astrabox:astrabox /data

USER astrabox
WORKDIR /home/astrabox

# Declarative metadata only; the actual bind/publish happens at `docker run`.
# 8000 is AstraBox (publish to host loopback); 80 is the embedded model
# gateway — do NOT publish it: sandboxes reach it over the container network,
# and that is its whole audience. The channel gateway binds container loopback
# only and therefore needs no EXPOSE entry.
EXPOSE 8000 80

# Persist local workspaces and generated keys across container restarts.
VOLUME ["/data"]

# Liveness probe hits the /healthz route the app factory always mounts
# (astrabox/api/app.py). Dependency-free (no curl in the image) via the
# guaranteed-present Python interpreter.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ASTRABOX_PORT','8000'), timeout=2).status==200 else 1)"]

# tini as PID 1, then the container entry point (astrabox/deploy/onebox.py).
#
# What it does depends only on configuration the operator was already setting:
#   * a backend already pointed at a lifecycle server someone else runs (or one
#     that needs none) — it `exec`s `astrabox serve`, replacing itself, and the
#     wrapper leaves no trace.
#   * open_sandbox with no ASTRABOX_SANDBOX_OPENAPI_BASE_URL — it starts the
#     lifecycle server on container loopback, waits for its /health, and runs
#     AstraBox in front of it, taking the container down if either process exits.
#
# CMD is the AstraBox command, so `docker run <image> serve --help` and friends
# still work: the entry point forwards whatever it is given.
ENTRYPOINT ["tini", "--", "python", "-m", "astrabox.deploy.onebox"]
CMD ["astrabox", "serve"]
