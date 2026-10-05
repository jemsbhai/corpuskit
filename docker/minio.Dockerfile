# Local development and CI services built from immutable community release source.
# Upstream removed its public images; keep source and base-image provenance pinned.
ARG GO_IMAGE_DIGEST=sha256:b8bae5bd9ba9b1f89b635c91c24cc75cea335a16fb5076310f38566fc674b1ec
ARG BUSYBOX_IMAGE_DIGEST=sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
FROM golang:1.24.7-bookworm@${GO_IMAGE_DIGEST} AS build-base
ENV CGO_ENABLED=0

FROM build-base AS server-build
RUN git init /src && cd /src && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin 07c3a429bfed433e49018cb0f78a52145d4bedeb \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "07c3a429bfed433e49018cb0f78a52145d4bedeb" \
    && go build -mod=readonly -trimpath -o /go/bin/minio .

FROM build-base AS client-build
RUN git init /src && cd /src && git remote add origin https://github.com/minio/mc.git \
    && git fetch --depth 1 origin 7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "7394ce0dd2a80935aded936b09fa12cbb3cb8096" \
    && go build -mod=readonly -trimpath -o /go/bin/mc .

FROM busybox:1.37.0@${BUSYBOX_IMAGE_DIGEST} AS runtime
COPY --from=build-base /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN mkdir -p /data /usr/share/licenses && chown 10001:10001 /data
ENV HOME=/tmp
USER 10001:10001

FROM runtime AS minio-server
COPY --from=server-build /go/bin/minio /usr/local/bin/minio
COPY --from=server-build /src/LICENSE /usr/share/licenses/minio-LICENSE
ENTRYPOINT ["/usr/local/bin/minio"]

FROM runtime AS minio-client
COPY --from=client-build /go/bin/mc /usr/local/bin/mc
COPY --from=client-build /src/LICENSE /usr/share/licenses/mc-LICENSE
ENTRYPOINT ["/usr/local/bin/mc"]
