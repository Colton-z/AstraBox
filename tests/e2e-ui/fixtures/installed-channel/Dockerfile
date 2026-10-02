# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS wheel
USER root
COPY --chmod=u=rwX,go=rX pyproject.toml /tmp/provider/pyproject.toml
COPY --chmod=u=rwX,go=rX astrabox_test_channel /tmp/provider/astrabox_test_channel
RUN python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/provider

FROM ${BASE_IMAGE}
USER root
COPY --from=wheel /tmp/wheels /tmp/wheels
RUN python -m pip install --no-index --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels
ARG RUNTIME_USER
USER ${RUNTIME_USER}
