"""In-process timeline pub/sub (ADR-005 D3): one asyncio queue per SSE subscriber.

``publish`` is only ever called from a post-commit hook (see ``Database.transaction``), so a
subscriber never sees an event that was not persisted. A subscriber whose queue overflows is
closed; its client reconnects with ``Last-Event-ID`` and replays from SQLite, so nothing is lost.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from nexus_contracts.events.timeline import TimelineEvent

log = logging.getLogger(__name__)

CLOSE = object()  # sentinel: end the stream


@dataclass(eq=False)
class Subscription:
    investigation_id: str
    queue: asyncio.Queue[object] = field(default_factory=lambda: asyncio.Queue(maxsize=1000))


class TimelineBroker:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def subscriber_count(self, investigation_id: str | None = None) -> int:
        if investigation_id is not None:
            return len(self._subs.get(investigation_id, ()))
        return sum(len(s) for s in self._subs.values())

    def subscribe(self, investigation_id: str) -> Subscription:
        sub = Subscription(investigation_id)
        if self._closed:
            sub.queue.put_nowait(CLOSE)
        else:
            self._subs.setdefault(investigation_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.investigation_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._subs[sub.investigation_id]

    def publish(self, event: TimelineEvent) -> None:
        for sub in list(self._subs.get(event.investigation_id, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "SSE subscriber too slow; closing its stream",
                    extra={"investigation_id": event.investigation_id},
                )
                self.unsubscribe(sub)
                self._force_close(sub)

    def close(self) -> None:
        """Graceful shutdown (ADR-005 D5): end every open stream."""
        self._closed = True
        for subs in list(self._subs.values()):
            for sub in list(subs):
                self._force_close(sub)
        self._subs.clear()

    @staticmethod
    def _force_close(sub: Subscription) -> None:
        while True:
            try:
                sub.queue.put_nowait(CLOSE)
                return
            except asyncio.QueueFull:
                sub.queue.get_nowait()  # make room for the sentinel
