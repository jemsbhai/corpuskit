# syntax=docker/dockerfile:1.12

ARG NODE_VERSION=24.18.1
ARG NODE_IMAGE_DIGEST=sha256:c2cc26d8f991c2db236ad51a61efee843c482372d6d22570787309d511694110
ARG NPM_VERSION=11.16.0
ARG OPENSSL_PACKAGE_VERSION=3.5.8-r0

FROM node:${NODE_VERSION}-alpine3.23@${NODE_IMAGE_DIGEST} AS dependencies

ARG NPM_VERSION
ARG OPENSSL_PACKAGE_VERSION
WORKDIR /app

RUN apk add --no-cache --upgrade \
        "libcrypto3=${OPENSSL_PACKAGE_VERSION}" \
        "libssl3=${OPENSSL_PACKAGE_VERSION}" \
    && npm install --global "npm@${NPM_VERSION}" --ignore-scripts \
    && test "$(npm --version)" = "${NPM_VERSION}"

COPY package.json package-lock.json .npmrc ./
COPY apps/web/package.json ./apps/web/package.json
RUN --mount=type=cache,target=/root/.npm \
    npm ci \
    && test "$(npm approve-scripts --allow-scripts-pending)" = \
        "No packages with unreviewed install scripts."

FROM dependencies AS builder

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY . .

RUN npm run build --workspace @corpuskit/web

FROM node:${NODE_VERSION}-alpine3.23@${NODE_IMAGE_DIGEST} AS runtime

ARG OPENSSL_PACKAGE_VERSION

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

WORKDIR /app

# npm is pinned and verified in the dependency/build stages. The standalone
# server needs only Node, so do not ship the build/package-manager toolchain.
RUN apk add --no-cache --upgrade \
        "libcrypto3=${OPENSSL_PACKAGE_VERSION}" \
        "libssl3=${OPENSSL_PACKAGE_VERSION}" \
    && rm -rf /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx

COPY --from=builder --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=builder --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=builder --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static

USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r => { if (!r.ok) process.exit(1) }).catch(() => process.exit(1))"]

CMD ["node", "apps/web/server.js"]
