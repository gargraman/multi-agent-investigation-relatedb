"""Per-task token budget (spec §8.5, S2 FR-030).

Plain version: the orchestrator hands each task a token allowance. Before every model call the
caller asks "could this call fit?" (an estimate); after the call it charges what the provider
reported. Either check failing ends the task ``FAILED (budget_exceeded)``.

The budget is enforced by the *caller* (the agent pipeline), not by providers: providers only
read :attr:`TokenBudget.remaining` to cap ``max_output_tokens``. That keeps live, replay and test
providers interchangeable and lets replay still exercise the budget path (ADR-006 E7).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from nexus_contracts.a2a.task import TaskUsage

CHARS_PER_TOKEN = 4
"""Heuristic for English/JSON prompts. Not a tokenizer: the post-call charge uses the provider's
real usage, so the estimate only has to stop obviously hopeless calls."""

DEFAULT_OUTPUT_RESERVE = 1024
"""Minimum output allowance a call must be able to afford (reasoning models spend output tokens
before they write the JSON)."""


class BudgetExceededError(Exception):
    """The estimate (pre-call) or the actual usage (post-call) does not fit the budget."""

    def __init__(self, message: str, *, usage: TaskUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass
class TokenBudget:
    """Tokens one task attempt may spend (``TaskRequest.token_budget``)."""

    limit: int
    spent: int = field(default=0)

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("token budget must be positive")

    @property
    def remaining(self) -> int:
        return max(self.limit - self.spent, 0)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token count of ``text`` (ceil(chars / 4))."""
        return math.ceil(len(text) / CHARS_PER_TOKEN)

    def estimate_call(self, *texts: str, output_reserve: int = DEFAULT_OUTPUT_RESERVE) -> int:
        """Estimated cost of one call: input estimate plus the output reserve."""
        return sum(self.estimate_tokens(t) for t in texts) + output_reserve

    def ensure_can_spend(self, estimate: int) -> None:
        """Pre-call check: raise if ``estimate`` does not fit what remains."""
        if estimate > self.remaining:
            raise BudgetExceededError(
                f"estimated {estimate} tokens exceeds remaining budget {self.remaining}"
            )

    def output_allowance(self, input_estimate: int, cap: int) -> int:
        """``max_output_tokens`` for a call: what remains after the input, at most ``cap``."""
        return max(min(cap, self.remaining - input_estimate), 16)

    def charge(self, usage: TaskUsage) -> None:
        """Post-call: record real usage; raise if the task is now over budget."""
        self.spent += usage.total_tokens
        if self.spent > self.limit:
            raise BudgetExceededError(
                f"spent {self.spent} tokens, budget {self.limit}", usage=usage
            )
