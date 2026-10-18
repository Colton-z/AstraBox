# syntax=docker/dockerfile:1

ARG UPSTREAM_EXECD_IMAGE=opensandbox/execd:v1.1.0

FROM scratch AS execd-source
ADD --checksum=sha256:c4d4d75c62acfb4684f2f78903d7664d0ad709da0d4fc7b6a6664dbb5af15324 https://codeload.github.com/opensandbox-group/OpenSandbox/tar.gz/48b0215f1bd097b31d0f022a44640e00c11ac49d /source.tar.gz

# Match the pinned supplier's compiler and both Linux binary variants. Its
# image still supplies bootstrap, isolation helpers and the supervisor.
FROM golang:1.25.9 AS builder
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends clang \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /source
RUN --mount=type=bind,from=execd-source,source=/source.tar.gz,target=/tmp/source.tar.gz \
    tar -xzf /tmp/source.tar.gz --strip-components=1
COPY --chmod=0644 containers/execd/stream-upload.patch /tmp/stream-upload.patch
COPY --chmod=0644 containers/execd/pty-output-drain.patch /tmp/pty-output-drain.patch
RUN git apply --check --unidiff-zero /tmp/stream-upload.patch /tmp/pty-output-drain.patch \
    && git apply --unidiff-zero /tmp/stream-upload.patch /tmp/pty-output-drain.patch
WORKDIR /source/components/execd
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false \
    -ldflags="-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=1.1.0+astrabox.stream-upload.pty-drain" \
    -o /build/execd ./main.go
RUN go run github.com/cilium/ebpf/cmd/bpf2go@v0.16.0 \
    -cc clang -no-strip -cflags "-Ipkg/ebpf/prog" -target "${TARGETARCH}" \
    -go-package ebpf -output-dir pkg/ebpf audit pkg/ebpf/prog/audit.bpf.c
RUN CGO_ENABLED=0 go build -tags ebpf -trimpath -buildvcs=false \
    -ldflags="-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=1.1.0+astrabox.stream-upload.pty-drain" \
    -o /build/execd-ebpf ./main.go

FROM ${UPSTREAM_EXECD_IMAGE}
COPY --from=builder --chmod=0755 /build/execd /execd
COPY --from=builder --chmod=0755 /build/execd-ebpf /execd-ebpf
COPY --from=builder --chmod=0644 /source/LICENSE /usr/share/doc/opensandbox/LICENSE
