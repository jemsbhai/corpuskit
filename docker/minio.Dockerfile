# Local development and CI services built from immutable community release source.
# Upstream removed its public images; keep source and base-image provenance pinned.
ARG GO_IMAGE_DIGEST=sha256:b8bae5bd9ba9b1f89b635c91c24cc75cea335a16fb5076310f38566fc674b1ec
ARG BUSYBOX_IMAGE_DIGEST=sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
FROM golang:1.24.7-bookworm@${GO_IMAGE_DIGEST} AS build-base
# The module proxy has returned HTTP/2 INTERNAL_ERROR responses in hosted CI.
# Use HTTP/1 for build downloads; the independent runtime stage keeps its defaults.
ENV CGO_ENABLED=0 GODEBUG=http2client=0

FROM build-base AS server-build
RUN --mount=type=cache,target=/go/pkg/mod,sharing=locked \
    git init /src && cd /src && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin 07c3a429bfed433e49018cb0f78a52145d4bedeb \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "07c3a429bfed433e49018cb0f78a52145d4bedeb" \
    && attempt=0 \
    && until timeout --signal=TERM --kill-after=10s 5m go mod download; do \
        attempt=$((attempt + 1)); \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep "$((attempt * 5))"; \
    done \
    && git diff --exit-code -- go.mod go.sum \
    && GOPROXY=off go mod verify \
    && GOPROXY=off go build -mod=readonly -trimpath -o /go/bin/minio . \
    && git diff --exit-code -- go.mod go.sum

FROM build-base AS client-build
RUN --mount=type=cache,target=/go/pkg/mod,sharing=locked \
    git init /src && cd /src && git remote add origin https://github.com/minio/mc.git \
    && git fetch --depth 1 origin 7394ce0dd2a80935aded936b09fa12cbb3cb8096 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "7394ce0dd2a80935aded936b09fa12cbb3cb8096" \
    && attempt=0 \
    && until timeout --signal=TERM --kill-after=10s 5m go mod download; do \
        attempt=$((attempt + 1)); \
        if [ "${attempt}" -ge 3 ]; then exit 1; fi; \
        sleep "$((attempt * 5))"; \
    done \
    && git diff --exit-code -- go.mod go.sum \
    && GOPROXY=off go mod verify \
    && GOPROXY=off go build -mod=readonly -trimpath -o /go/bin/mc . \
    && git diff --exit-code -- go.mod go.sum

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
