# syntax=docker/dockerfile:1
# ContextForge, the connector hub: the official release plus the patches in
# patches/. The patches are exported with `git format-patch` from the
# astrabox/<release> branch of https://github.com/Colton-z/mcp-context-forge,
# where they are rebased onto each new release; each names the upstream change
# that retires it. They are applied with `git apply`, so a release whose code
# moved under a patch fails this build rather than producing an image without
# the patch.
ARG CONTEXTFORGE_IMAGE=ghcr.io/ibm/mcp-context-forge:v1.0.11@sha256:e9639c030162d6cb2f9f188600dd69af8091ec62a7ce307d4a74f4d4f86873df

FROM ${CONTEXTFORGE_IMAGE} AS upstream

FROM alpine/git:2.47.2 AS patched
COPY --from=upstream /app/mcpgateway /src/mcpgateway
COPY --chmod=u=rwX,go=rX patches /patches
RUN cd /src && for patch in /patches/*.patch; do git apply --verbose "$patch"; done

FROM upstream
COPY --from=patched /src/mcpgateway /app/mcpgateway
# The hub runs as 10001 and keeps its database in the volume compose mounts at
# /data. A new named volume takes the owner of the image's directory, and
# without one Docker creates it for root, where the hub cannot create its
# database and never finishes starting.
USER root
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
