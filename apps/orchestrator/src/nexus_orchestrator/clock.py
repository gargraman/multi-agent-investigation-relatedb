"""Injectable time source (S3b). Anything that decides *when* (deadline checks, the driver tick,
dispatch backoff, ``now`` for state writes) takes a :class:`Clock`, so tests can run the
deadline/re-dispatch/reconcile paths on a fake clock instead of waiting in real time.

Not routed through the clock: the leader lock heartbeat, the Context Service create retrier and
the agent-card refresh loop. They are real-time housekeeping with no deadline semantics.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from nexus_orchestrator.ids import utcnow


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
