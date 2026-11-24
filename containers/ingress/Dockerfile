# syntax=docker/dockerfile:1

ARG UPSTREAM_INGRESS_IMAGE=opensandbox/ingress:release-1.2.1@sha256:832fb8e8ef55c8b038e4b0d83aa100d4ba320efe776796fe36832f9f9c479896

FROM scratch AS ingress-source
ADD --checksum=sha256:9b800800d52b4aeb0d47fbc0474eaa8aa7c649eaabd25dbc839c64a29d1c3a53 https://codeload.github.com/opensandbox-group/OpenSandbox/tar.gz/72e9feda4356a3087ae279af254ac67bb98c0129 /source.tar.gz

# Match the pinned supplier's compiler; its image supplies the runtime base.
FROM golang:1.25.9 AS builder
WORKDIR /source
RUN --mount=type=bind,from=ingress-source,source=/source.tar.gz,target=/tmp/source.tar.gz \
    tar -xzf /tmp/source.tar.gz --strip-components=1
COPY --chmod=0644 containers/ingress/full-duplex.patch /tmp/full-duplex.patch
RUN git apply --check --unidiff-zero /tmp/full-duplex.patch \
    && git apply --unidiff-zero /tmp/full-duplex.patch
WORKDIR /source/components/ingress
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false \
    -ldflags="-buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=1.2.1+astrabox.full-duplex" \
    -o /build/ingress ./main.go

FROM ${UPSTREAM_INGRESS_IMAGE}
COPY --from=builder --chmod=0755 /build/ingress /ingress
COPY --from=builder --chmod=0644 /source/LICENSE /usr/share/doc/opensandbox/LICENSE
