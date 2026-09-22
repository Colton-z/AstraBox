# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG TARGETARCH
RUN test "${TARGETARCH}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fuse3 util-linux \
    && rm -rf /var/lib/apt/lists/*

# The release archive is architecture-specific; other targets must fail closed.
RUN curl --fail --location --retry 3 \
      https://github.com/trapexit/mergerfs/releases/download/2.42.0/mergerfs-2.42.0-static-linux_amd64.tar.gz \
      --output /tmp/mergerfs.tar.gz \
    && echo '0cf8692e1687c8a1140c714966c6f5f4b498a1537f1a0bef5665082ecb35fc12  /tmp/mergerfs.tar.gz' | sha256sum --check \
    && tar -xzf /tmp/mergerfs.tar.gz -C / \
    && rm /tmp/mergerfs.tar.gz \
    && mkdir -p /usr/share/doc/mergerfs \
    && curl --fail --location \
      https://raw.githubusercontent.com/trapexit/mergerfs/ce3737ff3314ee0eb5edc38720b1a3f1fcb0e153/LICENSE \
      --output /usr/share/doc/mergerfs/LICENSE

COPY astrabox/core/service/orchestrator/runtime/storage/mergerfs_node.py /opt/astrabox/mergerfs_node.py
ENTRYPOINT ["python3", "-u", "/opt/astrabox/mergerfs_node.py"]
