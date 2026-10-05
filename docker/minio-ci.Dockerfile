# CI-only MinIO, built from the last published community release source.
# Upstream removed its public images; retain immutable source provenance.
ARG GO_IMAGE_DIGEST=sha256:40b223522c211e882863245ddbc812cbac8460b63b9112f4feee85c4514b6e41
FROM golang:1.24.7-bookworm@${GO_IMAGE_DIGEST} AS build
ENV CGO_ENABLED=0
RUN git init /src && cd /src && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin 07c3a429bfed433e49018cb0f78a52145d4bedeb \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "07c3a429bfed433e49018cb0f78a52145d4bedeb" \
    && go build -mod=readonly -trimpath -o /go/bin/minio .
FROM scratch
COPY --from=build /go/bin/minio /minio
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
USER 10001:10001
ENTRYPOINT ["/minio"]
