#!/usr/bin/env bash
# ADR-001 R5: re-run the Relate S0 spike suite INSIDE the Linux Context Service image, with the
# store on a Docker named volume (not a bind mount), before S1 closes.
#
# Usage: scripts/spike_linux.sh            (from the repo root; builds the image if needed)
#        C4_ROUNDS=20 scripts/spike_linux.sh
#
# The image already has relate-db (same pinned wheel as production) and the sha256-verified
# relate CLI. pytest and psutil are installed into a throwaway dir for this run only.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=nexus/context-service:dev
VOLUME=nexus-spike-store
docker compose -f infrastructure/docker/compose.yaml --env-file .env build context-service >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm --user root \
  -e DO_NOT_TRACK=1 -e RELATE_TELEMETRY=0 -e RELATE_CLI=/usr/local/bin/relate \
  -e C4_ROUNDS="${C4_ROUNDS:-20}" \
  -v "$PWD/spikes/relate:/spike:ro" \
  -v "$VOLUME:/spike-store" \
  --entrypoint sh "$IMAGE" -c '
    set -e
    cp -r /spike /work && rm -rf /work/store && ln -s /spike-store /work/store
    /usr/local/bin/python -m pip install -q --target /tmp/pt pytest psutil >/dev/null 2>&1
    uname -m; /app/.venv/bin/python -c "import importlib.metadata as m; print(\"relate-db\", m.version(\"relate-db\"))"
    relate --version || true
    cd /work && PYTHONPATH=/tmp/pt:/work /app/.venv/bin/python -m pytest tests -q -p no:cacheprovider
  '
docker volume rm "$VOLUME" >/dev/null
