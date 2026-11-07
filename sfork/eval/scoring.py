"""Score branches relative to the fork point, rank them, and say why the winner won (PLAN.md 6).

Each branch gets components in [0, 1] and a weighted total (`weights`, renormalized over the
components that apply to the task):

  tests      pass rate; with `regressions: penalty`, minus the share of tests that passed at the fork
             point and fail now
  perf       per benchmark, r / (1 + r) where r is the improvement ratio over the fork point
             (baseline / value when lower is better). 0.5 means "same as the fork point", 0.67 twice
             as good, 0.91 ten times. When the fork point has no number (e.g. the function wasn't
             written yet), r is measured against the best branch instead. Missing or failed runs: 0.
  diff_size  smallest diff among the branches / this diff (+10 lines smoothing), halved in proportion
             to the files touched outside `quality.expected`
  lint       1 / (1 + findings added since the fork point)

A branch is disqualified (score 0) when a gate fails, it errored, it edits a `quality.protected`
file, or (with `regressions: gate`, the default) a test that passed at the fork point now fails.
Ranking is (not disqualified, pass rate if `correctness_first`, score), stable on input order, so a
faster or tidier branch never beats a more correct one unless you turn `correctness_first` off.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import EvalConfig
from .evaluator import Evaluation, match_any

DIFF_SMOOTHING = 10


def score(evals: Sequence[Evaluation], cfg: EvalConfig, baseline: Evaluation | None = None) -> None:
    """Fill in components, score, regressions and disqualifications. Idempotent."""
    base = baseline if baseline is not None and baseline.error is None else None
    for e in evals:
        e.components, e.score, e.regressions, e.disqualified = {}, 0.0, [], []
        e.protected_touched, e.outside_expected, e.baseline_deltas = [], [], {}
        files = list((e.files_changed or {}).keys())
        e.protected_touched = [f for f in files if match_any(f, cfg.quality.protected)]
        if cfg.quality.expected:
            e.outside_expected = [f for f in files if not match_any(f, cfg.quality.expected)]
        if e.protected_touched:
            e.disqualified.append(f"edited protected files: {', '.join(e.protected_touched[:5])}")
        if base is not None and base.tests and e.tests:
            e.regressions = sorted(n for n, ok in base.tests.items() if ok and e.tests.get(n) is False)
            mode = cfg.tests.regressions if cfg.tests else "gate"
            if e.regressions and mode == "gate":
                e.disqualified.append(
                    f"regressed {len(e.regressions)} test(s) that passed at the fork point: "
                    + ", ".join(_short(n) for n in e.regressions[:5])
                )
    ok = [e for e in evals if e.gates_ok]

    # tests
    if cfg.tests is not None:
        mode = cfg.tests.regressions
        for e in evals:
            c = e.pass_rate
            if mode == "penalty" and e.total:
                c -= len(e.regressions) / e.total
            e.components["tests"] = max(0.0, c)

    # perf
    if cfg.benchmarks:
        for e in evals:
            e.components["perf"] = 0.0
        for bench in cfg.benchmarks:
            ref = base.metrics.get(bench.name) if base is not None else None
            if ref is None or ref <= 0:
                vals = [v for x in ok if (v := x.metrics.get(bench.name)) is not None and v > 0]
                ref = (min(vals) if bench.direction == "lower" else max(vals)) if vals else None
                vs = "best branch"
            else:
                vs = "fork point"
            for e in evals:
                v = e.metrics.get(bench.name)
                e.metrics.setdefault(bench.name, None)
                if ref is None or v is None or v <= 0:
                    continue
                r = ref / v if bench.direction == "lower" else v / ref
                e.components["perf"] += (r / (1 + r)) / len(cfg.benchmarks)
                if vs == "fork point":
                    e.baseline_deltas[bench.name] = f"{v:.4g} vs {ref:.4g} at the fork point ({_ratio(r)})"

    # diff size
    sized = [x for x in ok if x.diff_lines is not None]
    if cfg.quality.measure_diff and any(x.diff_lines is not None for x in evals):
        smallest = min((x.diff_lines or 0) for x in sized) if sized else 0
        for e in evals:
            if e.diff_lines is None:
                e.components["diff_size"] = 0.0
                continue
            c = (smallest + DIFF_SMOOTHING) / (e.diff_lines + DIFF_SMOOTHING)
            if e.files_changed and e.outside_expected:
                c *= 1 - 0.5 * len(e.outside_expected) / len(e.files_changed)
            e.components["diff_size"] = min(1.0, c)

    # lint
    if cfg.quality.lint is not None:
        ref = base.lint_findings if base is not None else None
        if ref is None:
            vals = [x.lint_findings for x in ok if x.lint_findings is not None]
            ref = min(vals) if vals else 0.0
        for e in evals:
            if e.lint_findings is None:
                e.components["lint"] = 0.0
                continue
            e.components["lint"] = 1 / (1 + max(0.0, e.lint_findings - ref))

    # total
    active = {k: w for k, w in cfg.weights.items() if w > 0 and any(k in e.components for e in evals)}
    wsum = sum(active.values())
    for e in evals:
        if base is not None and cfg.tests is not None:
            e.baseline_deltas["tests"] = (
                f"{e.passed}/{e.total} vs {base.passed}/{base.total} at the fork point"
            )
        if not e.gates_ok or not wsum:
            e.score = 0.0
            continue
        e.score = sum(w * e.components.get(k, 0.0) for k, w in active.items()) / wsum


def rank(
    evals: Sequence[Evaluation], cfg: EvalConfig, baseline: Evaluation | None = None
) -> list[Evaluation]:
    """Score, then return best first (stable: ties keep input order)."""
    score(evals, cfg, baseline)
    return sorted(evals, key=lambda e: e.rank_key(cfg.correctness_first), reverse=True)


def near_tie(a: Evaluation, b: Evaluation, cfg: EvalConfig) -> bool:
    if not (a.gates_ok and b.gates_ok):
        return False
    if cfg.correctness_first and round(a.pass_rate, 6) != round(b.pass_rate, 6):
        return False
    return abs(a.score - b.score) <= cfg.judge.margin


@dataclass
class Verdict:
    """The LLM judge's preference between the top two (kept out of the score; see judge.py)."""

    preferred: str  # node id
    reasons: str
    consistent: bool  # same answer with A/B swapped (or only one order asked)
    applied: bool  # did it change or confirm the order
    model: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Selection:
    ranked: list[Evaluation]
    why: str
    deciding: str  # only | disqualified | tests | <component> | judge | tie
    baseline: Evaluation | None = None
    verdict: Verdict | None = None

    @property
    def winner(self) -> Evaluation:
        return self.ranked[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner.label or self.winner.node_id,
            "winner_node": self.winner.node_id,
            "deciding": self.deciding,
            "why": self.why,
            "ranking": [
                {
                    "label": e.label or e.node_id,
                    "node": e.node_id,
                    "score": round(e.score, 4),
                    "qualified": e.gates_ok,
                    "components": {k: round(v, 4) for k, v in e.components.items()},
                }
                for e in self.ranked
            ],
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "judge": self.verdict.as_dict() if self.verdict else None,
        }


def explain(
    ranked: Sequence[Evaluation],
    cfg: EvalConfig | None = None,
    *,
    baseline: Evaluation | None = None,
    verdict: Verdict | None = None,
) -> tuple[str, str]:
    """(why, deciding factor). `why` is one paragraph a human can audit against the artifacts."""
    cfg = cfg or EvalConfig()
    if not ranked:
        return "no candidates", "none"
    w = ranked[0]
    name = _name(w)
    if len(ranked) == 1:
        return f"{name}: {w.summary()}", "only"
    r = ranked[1]
    if not w.gates_ok:
        why, deciding = "no branch qualified; kept the one with the most passing tests", "disqualified"
    elif not r.gates_ok:
        why, deciding = f"{_name(r)} (runner-up) was disqualified: {_dq(r)}", "disqualified"
    elif cfg.correctness_first and round(w.pass_rate, 6) > round(r.pass_rate, 6):
        why = f"more tests pass ({w.passed}/{w.total} vs {r.passed}/{r.total} for {_name(r)})"
        deciding = "tests"
    elif verdict is not None and verdict.applied:
        why = (
            f"scores were within {cfg.judge.margin} ({w.score:.3f} vs {r.score:.3f}), so the LLM judge "
            f"compared the top two and preferred {name}: {verdict.reasons.rstrip('. ')}"
        )
        deciding = "judge"
    elif w.score > r.score:
        k, _ = _biggest_edge(w, r, cfg)
        why = (
            f"higher score ({w.score:.3f} vs {r.score:.3f} for {_name(r)}), mostly from {_describe(k, w, r)}"
        )
        deciding = k
        if verdict is not None and not verdict.applied:
            why += "; the LLM judge was asked but gave different answers with A/B swapped, so it was ignored"
    else:
        why, deciding = "tie on every measure; kept the first", "tie"
    out = [f"{name} won: {why}."]
    if baseline is not None and baseline.error is None:
        out.append(f"Fork point: {baseline.summary()}.")
    for e in ranked:
        line = f"{_name(e)}: {e.summary()}"
        if e.regressions and e.gates_ok:
            line += f" (regressed: {', '.join(_short(n) for n in e.regressions[:3])})"
        out.append(line)
    return " | ".join(out), deciding


def _biggest_edge(w: Evaluation, r: Evaluation, cfg: EvalConfig) -> tuple[str, float]:
    active = {k: v for k, v in cfg.weights.items() if v > 0 and k in w.components}
    wsum = sum(active.values()) or 1
    edges = {k: wt * (w.components.get(k, 0) - r.components.get(k, 0)) / wsum for k, wt in active.items()}
    if not edges:
        return "score", 0.0
    k = max(edges, key=lambda x: edges[x])
    return k, edges[k]


def _describe(k: str, w: Evaluation, r: Evaluation) -> str:
    if k == "perf":
        bits = []
        for m, v in w.metrics.items():
            rv = r.metrics.get(m)
            if v is not None:
                bits.append(f"{m} {v:.4g} vs {'n/a' if rv is None else f'{rv:.4g}'}")
                if m in w.baseline_deltas:
                    bits[-1] += f"; {w.baseline_deltas[m]}"
        return "benchmarks (" + ", ".join(bits) + ")"
    if k == "diff_size":
        extra = f", {len(r.outside_expected)} file(s) outside the expected area" if r.outside_expected else ""
        return f"a smaller change ({w.diff_lines} vs {r.diff_lines} lines{extra})"
    if k == "lint":
        return f"fewer lint findings ({w.lint_findings:g} vs {r.lint_findings})"
    if k == "tests":
        return f"tests ({w.passed}/{w.total} vs {r.passed}/{r.total}, regressions {len(r.regressions)})"
    return k


def _dq(e: Evaluation) -> str:
    if e.error:
        return f"error ({e.error[:200]})"
    bad = [g for g, ok in e.gates.items() if not ok]
    reasons = ([f"gate {', '.join(bad)} failed"] if bad else []) + e.disqualified
    return "; ".join(reasons) or "disqualified"


def _name(e: Evaluation) -> str:
    return e.label or e.node_id


def _short(test: str) -> str:
    return test.split("::")[-1]


def _ratio(r: float) -> str:
    if r >= 1:
        return f"{r:.3g}x better"
    return f"{1 / r:.3g}x worse"


__all__ = ["Selection", "Verdict", "explain", "near_tie", "rank", "score"]
