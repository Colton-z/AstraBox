# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY tests/e2e/fixtures/session_mcp/personal_mcp.py /opt/astrabox/session-mcp-fixture/personal_mcp.py
COPY --chmod=u=rwX,go=rX tests/e2e/fixtures/session_mcp/plugin /opt/session-mcp-marketplace/plugins/fixture-identity
COPY tests/e2e/fixtures/session_mcp/marketplace.json /opt/session-mcp-marketplace/.claude-plugin/marketplace.json
RUN install -d -m 0700 -o 1000 -g 1000 /home/agent && \
    HOME=/home/agent CLAUDE_CONFIG_DIR=/home/agent/.claude \
    /usr/local/bin/claude plugin install fixture-identity \
    --marketplace /opt/session-mcp-marketplace --scope user --yes --json && \
    chown -R 1000:1000 /home/agent
