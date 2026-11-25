# syntax=docker/dockerfile:1.7
# Nexus frontend: build static assets with Node, serve with unprivileged nginx (spec §16.1/16.2).
# Build context: apps/frontend. Generated contract types (src/api/generated) are committed, so the
# build does not need contracts/. Multi-arch: the build stage runs on the builder's native
# platform (output is arch-independent); only the nginx stage is per-target.

ARG NODE_IMAGE=node:24.22.3-alpine3.24@sha256:116b800c3754b440ee0aa3ed2f20f5bf3d99a554bfd9adeea09ea8017f6e85f5
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30.6-alpine3.24@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03

FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
WORKDIR /app
ENV CI=true
COPY package.json package-lock.json ./
# Optional corporate CA for TLS-intercepting networks:
#   docker build --secret id=npm_ca,src=/path/to/ca.pem .
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=secret,id=npm_ca,required=false \
    if [ -s /run/secrets/npm_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/npm_ca; fi; \
    npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
ENV NEXUS_API_UPSTREAM=orchestrator:8000 \
    NGINX_RESOLVER=127.0.0.11
# Image default conf is replaced; the template is rendered by the image's envsubst entrypoint.
USER root
RUN rm -f /etc/nginx/conf.d/default.conf
COPY --chown=101:101 nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
