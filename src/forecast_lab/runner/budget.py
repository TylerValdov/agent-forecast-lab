"""Budget guard: stop cleanly before a prediction would exceed the dollar or token cap."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BudgetGuard:
    usd_cap: float
    token_cap: int
    usd_spent: float = 0.0
    tokens_spent: int = 0
    n_charges: int = 0
    stopped_reason: str | None = None

    def charge(self, usd: float, tokens: int) -> None:
        self.usd_spent += usd
        self.tokens_spent += tokens
        self.n_charges += 1

    def can_afford_next(self, safety: float = 2.0) -> bool:
        """Estimate the next unit of work as `safety` x the running mean cost."""
        if self.stopped_reason:
            return False
        mean_usd = self.usd_spent / self.n_charges if self.n_charges else 0.0
        mean_tok = self.tokens_spent / self.n_charges if self.n_charges else 0
        if self.usd_spent + safety * mean_usd > self.usd_cap:
            self.stopped_reason = f"usd budget ${self.usd_cap:.2f} reached"
        elif self.tokens_spent + safety * mean_tok > self.token_cap:
            self.stopped_reason = f"token budget {self.token_cap} reached"
        return self.stopped_reason is None
