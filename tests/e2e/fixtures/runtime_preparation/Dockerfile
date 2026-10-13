ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN mv /usr/local/bin/claude /usr/local/bin/claude.vendor
COPY --chmod=755 tests/e2e/fixtures/runtime_preparation/claude-gate.py /usr/local/bin/claude
