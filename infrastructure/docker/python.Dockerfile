# syntax=docker/dockerfile:1
# One image recipe for every Python service in the uv workspace (spec §7, §16.2).
#
#   docker build -f infrastructure/docker/python.Dockerfile \
#     --build-arg PACKAGE=nexus-context-service --build-arg MODULE=nexus_context_service .
#
# Each image contains only its own member plus nexus-contracts and nexus-shared (uv resolves
# exactly that closure from the lockfile). Base is Debian trixie: the relate CLI needs glibc ≥ 2.39
# (bookworm has 2.36; found by the ADR-001 R5 Linux re-run). Multi-arch: build with `docker buildx --platform
# linux/amd64,linux/arm64`. Base images are pinned by tag; digest pinning lands in S6 (§16.2).

ARG PYTHON_IMAGE=python:3.12-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6
ARG TARGETARCH

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG PACKAGE
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Whole workspace is needed for `--frozen` lock validation; .dockerignore keeps data/specs out.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

# Relate CLI for `relate clone` backups (ADR-001 R3): same version as relate-db, sha256-pinned.
# `ADD --checksum` makes BuildKit verify the download and cache on the digest, so rebuilds don't
# re-fetch and a tampered or wrong binary fails the build. One stage per arch, picked below.
FROM scratch AS relate-cli-amd64
ADD --checksum=sha256:03e05e7cf25eec498e328837a3b6636c7a4807410b989e4b72948f1ebc8a99a5 \
    --chmod=0755 \
    https://github.com/relatedb/relate/releases/download/v1.2.0/relate-linux-amd64 /relate

FROM scratch AS relate-cli-arm64
ADD --checksum=sha256:e73f39b2f0442b76f8a5e01194ff6ed69a8dd2637427c4f97ae9d4ae8f288859 \
    --chmod=0755 \
    https://github.com/relatedb/relate/releases/download/v1.2.0/relate-linux-arm64 /relate

FROM relate-cli-${TARGETARCH} AS relate-cli

FROM ${PYTHON_IMAGE} AS runtime
ARG MODULE
ARG WITH_RELATE_CLI=false
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    APP_MODULE="${MODULE}.main:app" \
    DO_NOT_TRACK=1 \
    RELATE_TELEMETRY=0
RUN groupadd --system --gid 10001 nexus \
 && useradd --system --uid 10001 --gid nexus --no-create-home nexus \
 && mkdir -p /data && chown nexus:nexus /data
COPY --from=build --chown=nexus:nexus /app/.venv /app/.venv
COPY --from=relate-cli /relate /opt/relate/relate
RUN if [ "${WITH_RELATE_CLI}" = "true" ]; then ln -s /opt/relate/relate /usr/local/bin/relate; \
    else rm -rf /opt/relate; fi
USER nexus
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"
# One worker by design (ADR-005 D1, ADR-001 single replica). exec form so SIGTERM reaches uvicorn.
CMD ["sh", "-c", "exec uvicorn \"$APP_MODULE\" --host 0.0.0.0 --port 8000 --workers 1 --no-access-log"]
