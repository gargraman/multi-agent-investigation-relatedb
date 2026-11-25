"""CA-aware TLS context for outbound LLM calls (S2 spec §10.3, G24).

Plain version: some hosts sit behind a TLS-intercepting proxy, so the OpenAI client must trust
that proxy's CA as well as the public roots. We start from certifi's public bundle and *append*
the extra CA, so a corporate-only bundle can never replace the public roots (which is what
setting ``SSL_CERT_FILE`` globally would risk).

A missing, empty or PEM-less extra file means "no extra CA" (compose mounts ``/dev/null``-backed
empty secrets on machines without a proxy).
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

import certifi

log = logging.getLogger(__name__)

PEM_MARKER = b"-----BEGIN CERTIFICATE-----"


def build_ssl_context(extra_ca_file: Path | None) -> ssl.SSLContext:
    """certifi defaults plus ``extra_ca_file`` when it holds at least one PEM certificate."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if extra_ca_file is None:
        return ctx
    try:
        data = extra_ca_file.read_bytes() if extra_ca_file.is_file() else b""
    except OSError:
        data = b""
    if PEM_MARKER not in data:
        log.info("no extra CA loaded", extra={"extra_ca_file": str(extra_ca_file)})
        return ctx
    ctx.load_verify_locations(cadata=data.decode("ascii", errors="ignore"))
    log.info("extra CA loaded", extra={"extra_ca_file": str(extra_ca_file)})
    return ctx
