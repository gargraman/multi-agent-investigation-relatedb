"""Id and timestamp helpers. Ids are ``inv_`` + a ULID (spec §13.2), so they sort by creation."""

import os
import time
from datetime import UTC, datetime

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """26-char Crockford base32: 48-bit ms timestamp + 80 random bits."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_investigation_id() -> str:
    return f"inv_{ulid()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO-8601 (microseconds, ``+00:00``) so text order == time order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
