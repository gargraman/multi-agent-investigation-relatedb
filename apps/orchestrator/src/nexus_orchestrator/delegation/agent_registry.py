"""Agent card cache: E4 objective lock and E8 health (S2 spec FR-008, FR-010; ADR-006).

Cards of the **enabled** agents are read at startup and every ``agent_card_refresh_seconds``
(30 s). If any enabled agent reports ``llm_mode=replay``, creating an investigation with a
non-template objective is refused with ``422 replay_objective_locked`` (replay recordings are
keyed on the template objective, so anything else would only end in ``replay_miss``).

Only enabled agents are read: they are the only ones that receive tasks. FR-008 says "any
registered agent"; a registered-but-disabled agent cannot cause a ``replay_miss``.

An unreadable card keeps its **last known** mode. Never read at all means "unknown", which does
not lock (fail-open): a live-mode demo must not be blocked by an agent that is briefly down,
and the worst case of failing open is a visible ``replay_miss`` task failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from nexus_contracts.a2a.task import AgentCard
from nexus_contracts.common import AgentId, LlmMode
from nexus_orchestrator.delegation.agent_client import AgentClient

log = logging.getLogger(__name__)


@dataclass
class AgentState:
    card: AgentCard | None = None  # last card successfully read
    reachable: bool = False  # did the most recent read succeed?
    detail: str = "not checked yet"

    @property
    def llm_mode(self) -> LlmMode | None:
        return self.card.llm_mode if self.card else None


class AgentRegistry:
    def __init__(self, client: AgentClient, agents: tuple[AgentId, ...], refresh: float) -> None:
        self.client = client
        self.agents = agents
        self.refresh_seconds = refresh
        self.states: dict[AgentId, AgentState] = {a: AgentState() for a in agents}
        self._task: asyncio.Task[None] | None = None

    @property
    def replay_locked(self) -> bool:
        return any(s.llm_mode is LlmMode.REPLAY for s in self.states.values())

    async def check(self, agent: AgentId) -> AgentState:
        outcome = await self.client.card(agent)
        state = self.states[agent]
        state.reachable = outcome.card is not None
        state.detail = outcome.detail
        if outcome.card is not None:
            if state.card is None or state.card.llm_mode != outcome.card.llm_mode:
                log.info(
                    "agent card read",
                    extra={"agent_id": agent.value, "llm_mode": outcome.card.llm_mode.value},
                )
            state.card = outcome.card
        return state

    async def refresh(self) -> None:
        await asyncio.gather(*(self.check(a) for a in self.agents))

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="agent-card-refresh")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent card refresh failed")
