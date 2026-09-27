# syntax=docker/dockerfile:1

# GitHub-hosted inputs are checksum-pinned stages of their own. A standalone
# build downloads them here and BuildKit verifies each digest; a build host that
# already holds the same verified bytes supplies them with
# `--build-context <stage>=DIR` instead of downloading again.
FROM scratch AS mergerfs-release
ADD --checksum=sha256:0cf8692e1687c8a1140c714966c6f5f4b498a1537f1a0bef5665082ecb35fc12 https://github.com/trapexit/mergerfs/releases/download/2.42.0/mergerfs-2.42.0-static-linux_amd64.tar.gz /mergerfs.tar.gz

FROM scratch AS mergerfs-license
ADD --checksum=sha256:5bf5ab72b9a8d4405b8e3c15aaea4e2eab39e18d105c15a1b24e6133f1b00892 https://raw.githubusercontent.com/trapexit/mergerfs/ce3737ff3314ee0eb5edc38720b1a3f1fcb0e153/LICENSE /LICENSE

FROM python:3.12-slim

ARG TARGETARCH
RUN test "${TARGETARCH}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fuse3 util-linux \
    && rm -rf /var/lib/apt/lists/*

# The release archive is architecture-specific; other targets must fail closed.
RUN --mount=type=bind,from=mergerfs-release,source=/mergerfs.tar.gz,target=/tmp/mergerfs.tar.gz \
    tar -xzf /tmp/mergerfs.tar.gz -C /
COPY --from=mergerfs-license --chmod=0644 /LICENSE /usr/share/doc/mergerfs/LICENSE

COPY --chmod=u=rwX,go=rX astrabox/core/service/orchestrator/runtime/storage/mergerfs_node.py /opt/astrabox/mergerfs_node.py
ENTRYPOINT ["python3", "-u", "/opt/astrabox/mergerfs_node.py"]
