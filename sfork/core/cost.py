"""Dollar cost of model calls, and a hard $ cap on a run (PLAN.md section 4.3).

`CostMeter` adds up what every call of a run cost. `BudgetedLLM` wraps an `LLM` and records each
call on a meter; with `enforce`, it refuses to start a call once the meter has reached its cap, by
raising `BudgetExceeded`. The agent loop turns that into a `budget` stop, so a branch that runs out
of money is evaluated like one that ran out of steps. The cap is checked before a call, so a run can
end above it by at most one call per concurrent branch; reports give the real spend.

Prices are USD per million tokens (Anthropic first-party API, 2026-10). Cache writes use the 5-minute
TTL rate (1.25x input).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .budget import BudgetExceeded

# model -> (input, output, cache read, cache write) in $ per million tokens
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}


def _usage_get(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    v = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
    return int(v or 0)


def cost_usd(model: str, usage: Any) -> float:
    """What one call (or a summed usage dict) cost. Unknown models are priced as Opus 5.5."""
    base = next((m for m in sorted(PRICES, key=len, reverse=True) if model.startswith(m)), "claude-opus-5-5")
    p_in, p_out, p_read, p_write = PRICES[base]
    return (
        _usage_get(usage, "input_tokens") * p_in
        + _usage_get(usage, "output_tokens") * p_out
        + _usage_get(usage, "cache_read_input_tokens") * p_read
        + _usage_get(usage, "cache_creation_input_tokens") * p_write
    ) / 1_000_000


@dataclass
class CostMeter:
    cap_usd: float | None = None
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
    refused: int = 0  # calls not started because the cap was reached

    def record(self, model: str, usage: Any) -> float:
        c = cost_usd(model, usage)
        self.spent_usd += c
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + c
        return c

    @property
    def exhausted(self) -> bool:
        return self.cap_usd is not None and self.spent_usd >= self.cap_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 5),
            "calls": self.calls,
            "refused_calls": self.refused,
            "by_model": {m: round(v, 5) for m, v in self.by_model.items()},
        }


class BudgetedLLM:
    """An `LLM` that meters every call and, with `enforce`, stops starting calls past the cap."""

    def __init__(self, inner: Any, meter: CostMeter, *, enforce: bool = True) -> None:
        self.inner = inner
        self.meter = meter
        self.enforce = enforce

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        if self.enforce and self.meter.exhausted:
            self.meter.refused += 1
            raise BudgetExceeded(
                f"$ budget reached: spent ${self.meter.spent_usd:.3f} of ${self.meter.cap_usd:.2f}"
            )
        resp = await self.inner.complete(
            model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        self.meter.record(getattr(resp, "model", None) or model, getattr(resp, "usage", None))
        return resp

    def __getattr__(self, name: str) -> Any:  # close() and anything else the inner client has
        return getattr(self.inner, name)
