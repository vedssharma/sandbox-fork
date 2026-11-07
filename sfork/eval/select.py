"""Pick the winner among evaluated branches: score, rank, judge near-ties, explain."""

from __future__ import annotations

from collections.abc import Sequence

from ..agent.llm import LLM
from .config import EvalConfig
from .evaluator import Evaluation
from .judge import judge
from .scoring import Selection, explain, near_tie, rank


async def select_winner(
    evals: Sequence[Evaluation],
    cfg: EvalConfig,
    *,
    baseline: Evaluation | None = None,
    llm: LLM | None = None,
    model: str | None = None,
    task: str = "",
) -> Selection:
    ranked = rank(evals, cfg, baseline)
    verdict = None
    if (
        cfg.judge.enabled
        and llm is not None
        and len(ranked) >= 2
        and near_tie(ranked[0], ranked[1], cfg)
        and (cfg.judge.model or model)
    ):
        verdict = await judge(
            llm,
            model=cfg.judge.model or model or "",
            task=task,
            first=ranked[0],
            second=ranked[1],
            cfg=cfg.judge,
        )
        if verdict.applied and verdict.preferred == ranked[1].node_id:
            ranked[0], ranked[1] = ranked[1], ranked[0]
    why, deciding = explain(ranked, cfg, baseline=baseline, verdict=verdict)
    return Selection(ranked=ranked, why=why, deciding=deciding, baseline=baseline, verdict=verdict)


__all__ = ["select_winner"]
