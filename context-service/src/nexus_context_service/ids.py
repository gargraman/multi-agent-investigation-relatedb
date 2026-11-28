"""Server-generated ids: ``<prefix>_`` + a monotonic ULID (spec §13.2, S2 spec §8.4).

The suffix fits the contract id patterns (``^fnd_[A-Za-z0-9_-]{1,64}$``, ``common.py``). Ids are
monotonic within this process, so the findings of one batch (which share ``created_at``) sort by
id in submission order, which is how the context read model orders them.

Prefixes: ``fnd_`` Finding, ``sub_`` Submission, ``asr_`` Assertion, ``aud_`` AuditEvent,
``asm_`` Assessment (the contract prefix for Assessment ids, so assertions do not reuse it).
"""

from __future__ import annotations

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_lock = threading.Lock()
_last = 0


def ulid() -> str:
    """26-char Crockford base32: 48-bit ms timestamp + 80 random bits, strictly increasing."""
    global _last
    with _lock:
        value = (int(time.time() * 1000) << _RANDOM_BITS) | int.from_bytes(os.urandom(10), "big")
        if value <= _last:  # same millisecond (or clock step back): keep the order
            value = _last + 1
        _last = value
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"
