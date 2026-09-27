# syntax=docker/dockerfile:1
#
# AstraBox Community — ALL-IN-ONE image: one `docker run` on a single machine.
#
# The released server image (containers/server/Dockerfile) plus PostgreSQL 17
# and Valkey, which run as supervised children on the container's loopback
# instead of as the Compose stack's `postgres` and `redis` services. The
# stack's two sandbox edges must not share this container's address, so the
# entry point creates them as sibling containers through the Docker socket.
# Design: docs/maintainers/all-in-one-image.md. The entry point is
# astrabox/deploy/all_in_one.py; it refuses every setting that would select a
# different topology, and names the Compose path instead.
#
# Built FROM the server image, by digest in the release workflow, so the
# platform bits are byte-identical to the released server image and the server
# Dockerfile gains no stage that would become the default target of an
# existing `docker build -f containers/server/Dockerfile`:
#
#   docker build --build-arg SERVER_IMAGE=<server image> \
#     -f containers/all-in-one/Dockerfile .
#
# (`make build-all-in-one-image` builds the server image first and passes it.)
ARG SERVER_IMAGE
FROM ${SERVER_IMAGE}

USER root

# Both from Debian's own trixie archive, signature-checked by apt:
#   * postgresql-17 — the major Compose runs (`postgres:17-alpine`). The server
#     image pins its base to trixie, so a base-image bump cannot move the
#     PostgreSQL major under an existing data volume; the codename check below
#     fails the build if that pin is ever lost. Debian's package creates a
#     default cluster on install unless told not to; the entry point creates
#     the cluster in the data volume instead.
#   * valkey-server — OpenSandbox's pool state store for Agent prewarming, the
#     role Redis has in Compose. Valkey is BSD-3-Clause.
# Their licence texts are installed under /usr/share/doc/<package>/copyright.
RUN set -eux; \
    . /etc/os-release; \
    test "$VERSION_CODENAME" = trixie; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-common; \
    sed -ri 's/^#?[[:space:]]*create_main_cluster[[:space:]]*=.*$/create_main_cluster = false/' \
      /etc/postgresql-common/createcluster.conf; \
    grep -qx 'create_main_cluster = false' /etc/postgresql-common/createcluster.conf; \
    apt-get install -y --no-install-recommends postgresql-17 valkey-server; \
    rm -rf /var/lib/apt/lists/*; \
    test -x /usr/lib/postgresql/17/bin/postgres; \
    test -x /usr/bin/valkey-server; \
    test -z "$(ls -A /var/lib/postgresql/17 2>/dev/null)"

# The database initialisation Compose runs, shared so both shapes create the
# same roles and databases (the Casdoor role only where team login runs).
# Every file this image adds is read by the unprivileged `astrabox` user, so
# its mode is set here rather than inherited from the build context: a
# checkout made under a restrictive umask would otherwise ship it root-only.
# The directories exist first because COPY --chmod gives a directory it
# creates the same mode, and a 0644 directory cannot be entered.
RUN install -d -m 0755 /opt/astrabox/postgres /opt/astrabox/sandbox-edge
COPY --chmod=0644 containers/postgres/init-databases.sh /opt/astrabox/postgres/init-databases.sh

# The two sandbox edges' configuration, the files Compose mounts into its
# `sandbox-edge` and `sandbox-dns-edge` services. The entry point copies them
# into the edge containers it creates through the Docker socket.
COPY --chmod=0644 containers/sandbox-edge/default.conf.template containers/sandbox-edge/dns-edge.nginx.conf \
     /opt/astrabox/sandbox-edge/

# The entry point starts as root only to give the runtime user the mounted
# Docker socket's group, take ownership of an empty /data and create the
# PostgreSQL socket directory under /run/astrabox; it then drops to
# `astrabox` before any service starts. No `--group-add` or `--user` at
# `docker run`.
RUN install -d -o astrabox -g astrabox -m 0700 /run/astrabox

ENTRYPOINT ["tini", "--", "python", "-m", "astrabox.deploy.all_in_one"]
CMD ["astrabox", "serve"]
