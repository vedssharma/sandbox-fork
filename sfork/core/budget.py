"""Per-run hard caps (PLAN.md section 4.3). Token and $ caps arrive with the agent in Phase 3."""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Budget:
    max_running_sandboxes: int = 6  # paused VMs release CPU/RAM quota; Tier 1 is 10 vCPU
    max_depth: int = 10
    max_fanout: int = 4

    def check_fork(self, *, running_sandboxes: int, parent_depth: int, n: int) -> None:
        if n < 1:
            raise ValueError("fork needs at least one child")
        if n > self.max_fanout:
            raise BudgetExceeded(f"fan-out {n} exceeds max_fanout={self.max_fanout}")
        if parent_depth + 1 > self.max_depth:
            raise BudgetExceeded(f"depth {parent_depth + 1} exceeds max_depth={self.max_depth}")
        if running_sandboxes + n > self.max_running_sandboxes:
            raise BudgetExceeded(
                f"{running_sandboxes} running + {n} new sandboxes exceeds "
                f"max_running_sandboxes={self.max_running_sandboxes}"
            )
