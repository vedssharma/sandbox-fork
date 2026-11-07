"""LLM judge for near-ties (PLAN.md 6, step 5).

Only called when the top two branches qualify, pass the same tests, and score within
`judge.margin`. It reads the task, both diffs and both evaluation summaries, and answers through a
`verdict` tool. With `both_orders` (the default) it is asked twice with A and B swapped, and its
answer only counts if it names the same branch both times, which cancels position bias. The verdict
is recorded next to the score, never folded into it, so the deterministic ranking stays reproducible.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..agent.llm import LLM, block_get
from .config import Judge
from .evaluator import Evaluation
from .scoring import Verdict

SYSTEM = """\
You review two candidate solutions to the same software task. Both already pass the same automated \
checks, so decide on what the checks may miss: does the change do what the task actually asks, is \
it safe (data integrity, transactions, failure modes, irreversible steps), is it maintainable, and \
does it avoid unrelated edits. Be concrete. Answer by calling the verdict tool once."""

VERDICT_TOOL = {
    "name": "verdict",
    "description": "Record which candidate is better and why.",
    "input_schema": {
        "type": "object",
        "properties": {
            "better": {"type": "string", "enum": ["A", "B"]},
            "reasons": {
                "type": "string",
                "description": (
                    "Two to four sentences naming the concrete differences that decided it. Describe "
                    "each candidate by what its change does, not by its letter."
                ),
            },
        },
        "required": ["better", "reasons"],
        "additionalProperties": False,
    },
}


async def judge(
    llm: LLM,
    *,
    model: str,
    task: str,
    first: Evaluation,
    second: Evaluation,
    cfg: Judge,
) -> Verdict:
    """Ask which of the top two is better. `first` is the deterministic winner."""
    orders = [(first, second), (second, first)] if cfg.both_orders else [(first, second)]
    answers = await asyncio.gather(*(_ask(llm, model, task, a, b, cfg) for a, b in orders))
    calls, picks = [], []
    for (a, b), (better, reasons, raw) in zip(orders, answers, strict=True):
        pick = None if better is None else (a if better == "A" else b)
        picks.append(pick)
        calls.append(
            {
                "A": a.label or a.node_id,
                "B": b.label or b.node_id,
                "better": better,
                "reasons": reasons,
                "raw": raw[:2000],
            }
        )
    consistent = all(p is not None for p in picks) and len({p.node_id for p in picks if p}) == 1
    chosen = picks[0] if consistent and picks[0] is not None else first
    reasons = next(
        (c["reasons"] for c, p in zip(calls, picks, strict=True) if p is chosen and c["reasons"]), ""
    )
    return Verdict(
        preferred=chosen.node_id,
        reasons=reasons,
        consistent=consistent,
        applied=consistent,
        model=model,
        calls=calls,
    )


async def _ask(
    llm: LLM, model: str, task: str, a: Evaluation, b: Evaluation, cfg: Judge
) -> tuple[str | None, str, str]:
    prompt = (
        f"<task>\n{task.strip()}\n</task>\n\n"
        + _candidate("A", a, cfg.max_diff_chars)
        + "\n\n"
        + _candidate("B", b, cfg.max_diff_chars)
        + "\n\nWhich candidate is the better solution to the task? Call the verdict tool."
    )
    try:
        resp = await llm.complete(
            model=model,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[VERDICT_TOOL],
            max_tokens=4000,
        )
    except Exception as e:  # a failed judge leaves the deterministic order in place
        return None, "", f"{type(e).__name__}: {e}"
    raw: list[str] = []
    for blk in resp.content:
        if block_get(blk, "type") == "tool_use" and block_get(blk, "name") == "verdict":
            args: dict[str, Any] = block_get(blk, "input") or {}
            better = args.get("better")
            if better in ("A", "B"):
                return better, str(args.get("reasons", "")).strip(), str(args)
        if block_get(blk, "type") == "text":
            raw.append(block_get(blk, "text") or "")
    return None, "", "no verdict: " + " ".join(raw)


def _candidate(tag: str, e: Evaluation, limit: int) -> str:
    diff = e.diff or "(no diff captured)"
    if len(diff) > limit:
        diff = diff[:limit] + "\n...[diff truncated]"
    return f'<candidate id="{tag}">\n<checks>{_checks(e)}</checks>\n<diff>\n{diff}\n</diff>\n</candidate>'


def _checks(e: Evaluation) -> str:
    """What the automated checks saw, without the score (so the judge doesn't just echo it)."""
    parts = [f"tests {e.passed}/{e.total}"]
    parts += [f"{k}={'n/a' if v is None else f'{v:.4g}'}" for k, v in e.metrics.items()]
    if e.diff_lines is not None:
        parts.append(f"diff {e.diff_lines} lines in {len(e.files_changed or {})} files")
    if e.lint_findings is not None:
        parts.append(f"lint findings {e.lint_findings:g}")
    return ", ".join(parts)


__all__ = ["VERDICT_TOOL", "judge"]
