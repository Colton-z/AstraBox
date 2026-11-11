# syntax=docker/dockerfile:1
# The e2e image for a dsh-superagent Assistant that works from its own Source: AstraBox's
# image plus the to-do Source package, built as docs/assistants.md ("Your own Sources")
# tells an organization to build its own.
#
#   docker build --build-arg SUPERAGENT_IMAGE=<sandbox-dsh-superagent image> \
#     -t <tag> tests/e2e-ui/fixtures/superagent-todo-source
ARG SUPERAGENT_IMAGE
FROM ${SUPERAGENT_IMAGE}
COPY --chmod=u=rwX,go=rX package.json /opt/astrabox/dsh-plugins/astrabox-e2e-todo-source/package.json
COPY --chmod=u=rwX,go=rX lib /opt/astrabox/dsh-plugins/astrabox-e2e-todo-source/lib
COPY --chmod=0444 50-todo.yml /opt/astrabox/superagent/patches.d/50-todo.yml
RUN /usr/local/bin/astrabox-dsh-superagent-boot-check --source github --source todo
