"""Generate per-service bearer tokens into the repo-root ``.env`` (spec §14.1, ``make secrets``).

Usage:
    python scripts/gen_secrets.py           # create .env, or add any missing token keys
    python scripts/gen_secrets.py --rotate  # replace every token

Existing non-token lines (e.g. OPENAI_API_KEY) are preserved. Values are never printed.
The file is created with mode 0600 and is git-ignored.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
SERVICES = ["orchestrator", "context-service", "sigint-agent", "geoint-agent", "analysis-agent"]


def key(service: str) -> str:
    return "NEXUS_TOKEN_" + service.upper().replace("-", "_")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rotate", action="store_true", help="replace existing tokens")
    args = parser.parse_args()

    lines = ENV.read_text().splitlines() if ENV.exists() else []
    present = {ln.split("=", 1)[0] for ln in lines if "=" in ln and not ln.startswith("#")}
    wanted = {key(s) for s in SERVICES}
    if args.rotate:
        lines = [ln for ln in lines if ln.split("=", 1)[0] not in wanted]
        present -= wanted
    added = [k for k in sorted(wanted) if k not in present]
    lines += [f"{k}={secrets.token_urlsafe(48)}" for k in added]

    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    print(f"{ENV.name}: {len(added)} token(s) written, {len(wanted) - len(added)} kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
