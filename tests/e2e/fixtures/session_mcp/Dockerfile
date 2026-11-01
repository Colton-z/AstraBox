# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
ARG PLUGIN_CACHE_HASH
ARG PLUGIN_SHA
ARG PLUGIN_CACHE_TARGET
COPY --chmod=0644 tests/e2e/fixtures/session_mcp/personal_mcp.py /opt/astrabox/session-mcp-fixture/personal_mcp.py
COPY --chmod=u=rwX,go=rX tests/e2e/fixtures/session_mcp/plugin ${PLUGIN_CACHE_TARGET}
RUN test -n "$PLUGIN_CACHE_HASH" && test -n "$PLUGIN_SHA" && test -n "$PLUGIN_CACHE_TARGET" && \
    git -C "$PLUGIN_CACHE_TARGET" init --initial-branch=main && \
    git -C "$PLUGIN_CACHE_TARGET" add . && \
    GIT_AUTHOR_DATE=2026-10-26T00:00:00Z GIT_COMMITTER_DATE=2026-10-26T00:00:00Z \
    git -C "$PLUGIN_CACHE_TARGET" -c user.name=Fixture -c user.email=fixture@example.invalid \
    commit -m 'Read-only identity fixture' && \
    test "$(git -C "$PLUGIN_CACHE_TARGET" rev-parse HEAD)" = "$PLUGIN_SHA" && \
    printf '%s\n' "$PLUGIN_CACHE_HASH" > /opt/conversation-runtime/claude-plugin-repos-cache/.plugin-cache-hash
