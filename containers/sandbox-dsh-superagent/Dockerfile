# syntax=docker/dockerfile:1
# AstraBox — dsh-superagent, an Assistant backend.
#
# The DeepSeek Harness image plus the dsh-superagent bundle: one resident harness per
# Assistant whose own plugin polls GitHub, routes requests in its main Session and runs
# Executor Sessions, supervised in the bundle's own panel. AstraBox supplies the box, the
# model route, the agent account's credential and the browser's way to the panel.
#
# Build from the repository root:
#   docker build -f containers/sandbox-dsh-superagent/Dockerfile \
#     --build-arg DSH_IMAGE=astrabox/sandbox-deepseek-harness:latest .
ARG DSH_IMAGE=astrabox/sandbox-deepseek-harness:latest
FROM ${DSH_IMAGE}

USER root

# The bundle is pinned to a commit: its database migrations run forward only, so a box
# must never meet an older bundle than the state it restores.
ARG SUPERAGENT_REPOSITORY=https://github.com/deepseek-ai/dsh-superagent-prototype.git
ARG SUPERAGENT_COMMIT=8c32a9dc5d72c033816c6a8110b37f40cf94fba4
ARG SUPERAGENT_PREFIX=/opt/dsh-superagent

# The bundle drives the agent account through GitHub CLI 2.48 or later
# (docs/006-deployment.md, Prerequisites); the base image's distribution package
# is older. The release archive is verified against the release's checksums.
ARG GH_VERSION=2.103.3
ARG GH_SHA256_AMD64=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
ARG GH_SHA256_ARM64=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum="$GH_SHA256_AMD64" ;; \
      arm64) sum="$GH_SHA256_ARM64" ;; \
      *) echo "no GitHub CLI build for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    echo "$sum  /tmp/gh.tgz" | sha256sum -c -; \
    tar -xzf /tmp/gh.tgz -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    test "$(/usr/local/bin/gh --version | head -1 | cut -d' ' -f3)" = "$GH_VERSION"

COPY --chmod=0444 containers/sandbox-dsh-superagent/resolve-bundle.py /tmp/resolve-bundle.py
# The patch over the pinned commit, offered upstream: GitHub becomes optional (an installation
# whose GitHub Source names no repository or CLI runs its other Sources), and the Source
# plugin interface is exported as `dsh-superagent/sources` for plugins in their own package.
COPY --chmod=0444 containers/sandbox-dsh-superagent/upstream-changes.patch /tmp/upstream-changes.patch

# Build the bundle with the pnpm the harness image pins, then make its host-side imports
# resolve to the harness this image runs. The bundle's lockfile carries the harness
# packages it was developed against for its own build and tests; loaded from there, the
# plugin would run on a second copy of the harness core beside the one booting it.
#
# The harness resolves a bundle's bare package names from the profile directory under
# DSH_HOME, which exists only in a booted box, so the bundle's own patch layer is written
# out with its names resolved to the files its package exports (see resolve-bundle.py).
RUN set -eux; \
    git clone -q "${SUPERAGENT_REPOSITORY}" "${SUPERAGENT_PREFIX}"; \
    git -C "${SUPERAGENT_PREFIX}" checkout -q "${SUPERAGENT_COMMIT}"; \
    git -C "${SUPERAGENT_PREFIX}" apply /tmp/upstream-changes.patch; \
    rm /tmp/upstream-changes.patch; \
    rm -rf "${SUPERAGENT_PREFIX}/.git"; \
    cd "${SUPERAGENT_PREFIX}"; \
    /opt/dsh/node_modules/.bin/pnpm install --frozen-lockfile; \
    /opt/dsh/node_modules/.bin/pnpm build; \
    rm -rf node_modules/@deepseek-ai; \
    ln -s /opt/dsh/node_modules/@deepseek-ai node_modules/@deepseek-ai; \
    python3 /tmp/resolve-bundle.py "${SUPERAGENT_PREFIX}" /opt/dsh/superagent-bundle.patch.yml; \
    rm /tmp/resolve-bundle.py; \
    chmod -R a+rX "${SUPERAGENT_PREFIX}" /opt/dsh/superagent-bundle.patch.yml

COPY --chmod=0444 containers/sandbox-dsh-superagent/superagent.patch.yml /opt/dsh/superagent.patch.yml
COPY --chmod=u=rwX,go=rX containers/sandbox-dsh-superagent/panel-hop.conf /opt/astrabox/superagent/panel-hop.conf
COPY --chmod=u=rwX,go=rX containers/sandbox-dsh-superagent/panel-landing /opt/astrabox/dsh-panel-landing
COPY --chmod=0555 containers/sandbox-dsh-superagent/gh-agent /usr/local/bin/gh-agent

# The Assistant's state: the shared runtime-state protocol and this bundle's
# layout of it (superagent_state.py), plus the mirror driver it runs under.
COPY --chmod=u=rwX,go=rX \
     astrabox/core/service/orchestrator/runtime/assistant-state/astrabox_state_transport.py \
     astrabox/core/service/orchestrator/runtime/assistant-state/astrabox_state_gate.py \
     astrabox/core/service/orchestrator/runtime/assistant-state/astrabox_state_mirror.py \
     containers/sandbox-dsh-superagent/superagent_state.py \
     containers/sandbox-dsh-superagent/superagent_sqlite.mjs \
     /opt/astrabox/superagent/
COPY --chmod=0555 astrabox/core/service/orchestrator/runtime/astrabox-transcript-mirror \
     /usr/local/bin/astrabox-transcript-mirror
COPY --chmod=0555 astrabox/core/service/orchestrator/runtime/provision-assistant-profile \
     /usr/local/bin/astrabox-provision-assistant-profile
COPY --chmod=0444 containers/sandbox-dsh-superagent/composition.sh /opt/astrabox/superagent/composition.sh
COPY --chmod=0555 containers/sandbox-dsh-superagent/astrabox-dsh-superagent-serve \
     containers/sandbox-dsh-superagent/astrabox-dsh-superagent-boot-check \
     containers/sandbox-dsh-superagent/astrabox-dsh-superagent-probe \
     containers/sandbox-dsh-superagent/astrabox-dsh-superagent-gh-login \
     /usr/local/bin/

# This image runs one harness per Assistant, started by its own programs once
# the platform has provisioned the Assistant's account; the base image's
# box-level harness, its platform forward and its conversation-log mirror serve
# Agent conversations and are not part of it.
RUN rm /opt/gem/supervisord/supervisord.dsh.conf
COPY --chmod=0444 containers/sandbox-dsh-superagent/supervisord.dsh-superagent.conf \
     /opt/gem/supervisord/supervisord.dsh-superagent.conf
ENV ASTRABOX_TRANSCRIPT_MIRROR_ROOT= \
    ASTRABOX_TRANSCRIPT_MIRROR_GLOB= \
    ASTRABOX_TRANSCRIPT_MIRROR_NAMESPACE=
RUN set -eux; \
    bash -n /usr/local/bin/astrabox-dsh-superagent-serve; \
    bash -n /usr/local/bin/astrabox-dsh-superagent-boot-check; \
    bash -n /opt/astrabox/superagent/composition.sh; \
    bash -n /usr/local/bin/astrabox-dsh-superagent-gh-login; \
    bash -n /usr/local/bin/astrabox-provision-assistant-profile; \
    python3 -m py_compile /usr/local/bin/astrabox-dsh-superagent-probe; \
    cd /opt/astrabox/superagent && runuser -u nobody -- python3 -c 'import superagent_state; assert superagent_state.LAYOUT.tag'; \
    probe="$(mktemp -d)"; \
    node --no-warnings -e "const {DatabaseSync}=require('node:sqlite'); const d=new DatabaseSync('$probe/a.sqlite'); d.exec(\"create table t(j text, v text as (j->>'x'))\"); d.close()"; \
    node --no-warnings /opt/astrabox/superagent/superagent_sqlite.mjs snapshot "$probe/a.sqlite" "$probe/b.sqlite"; \
    node --no-warnings /opt/astrabox/superagent/superagent_sqlite.mjs check "$probe/b.sqlite"; \
    rm -rf "$probe"; \
    nginx -V 2>&1 | grep -q -- --with-http_auth_request_module; \
    command -v git tmux

# An image built on this one adds its own Source plugins here: their loader layers under
# patches.d, the plugins under dsh-plugins, where `dsh-superagent/sources` resolves to
# this bundle (composition.sh).
RUN set -eux; \
    mkdir -p /opt/astrabox/superagent/patches.d /opt/astrabox/dsh-plugins/node_modules; \
    ln -s "${SUPERAGENT_PREFIX}" /opt/astrabox/dsh-plugins/node_modules/dsh-superagent; \
    chmod -R a+rX /opt/astrabox/superagent/patches.d /opt/astrabox/dsh-plugins

# The composition this image boots must load against the harness it ships.
RUN /usr/local/bin/astrabox-dsh-superagent-boot-check --source github
