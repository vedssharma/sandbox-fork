"""Run gates, tests and benchmarks inside a branch's sandbox, and rank branches.

Ranking is lexicographic, so a faster branch can never beat a more correct one:
  1. all gates pass (else disqualified),
  2. test pass rate,
  3. benchmark score: each metric normalized against the best branch (best/value for "lower",
     value/best for "higher"), averaged; a missing or failed metric counts as 0.
Remaining ties go to the branch that finished first in the input order.
"""

from __future__ import annotations

import json
import re
import shlex
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..daytona.base import ExecResult
from .config import Benchmark, EvalConfig, TestSpec

Exec = Callable[[str, int], Awaitable[ExecResult]]
RAW_LIMIT = 4000


@dataclass
class Evaluation:
    node_id: str
    label: str | None = None
    gates: dict[str, bool] = field(default_factory=dict)
    passed: int = 0
    total: int = 0
    metrics: dict[str, float | None] = field(default_factory=dict)
    perf_score: float = 0.0
    error: str | None = None
    seconds: float = 0.0
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def gates_ok(self) -> bool:
        return self.error is None and all(self.gates.values())

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def rank_key(self) -> tuple[int, float, float]:
        return (int(self.gates_ok), round(self.pass_rate, 6), round(self.perf_score, 6))

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        bad = [g for g, ok in self.gates.items() if not ok]
        parts = [f"gates failed: {', '.join(bad)}"] if bad else []
        parts.append(f"tests {self.passed}/{self.total}")
        for name, v in self.metrics.items():
            parts.append(f"{name}={'n/a' if v is None else f'{v:.4g}'}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "gates": self.gates,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "metrics": self.metrics,
            "perf_score": self.perf_score,
            "error": self.error,
            "seconds": round(self.seconds, 3),
            "raw": self.raw,
        }


def in_dir(workdir: str, cmd: str) -> str:
    return f"cd {shlex.quote(workdir)} && {cmd}"


async def evaluate(exec_: Exec, cfg: EvalConfig, *, node_id: str, label: str | None = None) -> Evaluation:
    """Run the eval suite through `exec_` (bound to one sandbox). Never raises for command failures."""
    ev = Evaluation(node_id=node_id, label=label)
    t0 = time.monotonic()
    try:
        for gate in cfg.gates:
            res = await exec_(in_dir(cfg.workdir, gate.cmd), gate.timeout)
            ev.gates[gate.name] = res.ok
            ev.raw[f"gate:{gate.name}"] = _clip(res.output)
        if cfg.tests is not None:
            res = await exec_(in_dir(cfg.workdir, cfg.tests.cmd), cfg.tests.timeout)
            ev.raw["tests"] = _clip(res.output)
            ev.passed, ev.total = parse_tests(cfg.tests, res)
        for bench in cfg.benchmarks:
            values = []
            for i in range(max(1, bench.repeats)):
                res = await exec_(in_dir(cfg.workdir, bench.cmd), bench.timeout)
                ev.raw[f"bench:{bench.name}:{i}"] = _clip(res.output)
                v = parse_metric(bench, res)
                if v is None:
                    values = []
                    break
                values.append(v)
            ev.metrics[bench.name] = statistics.median(values) if values else None
    except Exception as e:  # sandbox gone, backend error: the branch just loses
        ev.error = f"{type(e).__name__}: {e}"
    ev.seconds = time.monotonic() - t0
    return ev


def rank(evals: Sequence[Evaluation], benchmarks: Sequence[Benchmark]) -> list[Evaluation]:
    """Fill in perf_score across the candidates and return them best first (stable)."""
    for e in evals:
        e.perf_score = 0.0
    for bench in benchmarks:
        vals = [e.metrics.get(bench.name) for e in evals if e.gates_ok]
        vals = [v for v in vals if v is not None and v > 0]
        best = (min(vals) if bench.direction == "lower" else max(vals)) if vals else None
        for e in evals:
            v = e.metrics.get(bench.name)
            if best is None or v is None or v <= 0:
                e.metrics.setdefault(bench.name, None)
                continue
            e.perf_score += (best / v if bench.direction == "lower" else v / best) / len(benchmarks)
    return sorted(evals, key=lambda e: e.rank_key(), reverse=True)


def explain(ranked: Sequence[Evaluation]) -> str:
    if not ranked:
        return "no candidates"
    w = ranked[0]
    if len(ranked) == 1:
        return f"{w.label or w.node_id}: {w.summary()}"
    r = ranked[1]
    if w.gates_ok and not r.gates_ok:
        why = "the runner-up failed a gate or errored"
    elif w.pass_rate > r.pass_rate:
        why = f"higher test pass rate ({w.pass_rate:.0%} vs {r.pass_rate:.0%})"
    elif w.perf_score > r.perf_score:
        why = f"same pass rate, better benchmarks (perf {w.perf_score:.2f} vs {r.perf_score:.2f})"
    else:
        why = "tie; kept the first"
    return f"{w.label or w.node_id} won: {why}. " + " | ".join(
        f"{e.label or e.node_id}: {e.summary()}" for e in ranked
    )


# --- parsers -----------------------------------------------------------------------------------

_PYTEST = re.compile(r"(\d+) (passed|failed|error|errors)\b")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _last_json(output: str) -> dict[str, Any] | None:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def parse_tests(spec: TestSpec, res: ExecResult) -> tuple[int, int]:
    if spec.parse == "json":
        obj = _last_json(res.output)
        if obj and isinstance(obj.get("passed"), int) and isinstance(obj.get("total"), int):
            return obj["passed"], obj["total"]
        return 0, 1
    if spec.parse == "pytest":
        counts = {"passed": 0, "failed": 0, "error": 0}
        for n, kind in _PYTEST.findall(res.output):
            counts["error" if kind.startswith("error") else kind] += int(n)
        total = sum(counts.values())
        return (counts["passed"], total) if total else (0, 1)
    if spec.parse == "exit":
        return (1, 1) if res.ok else (0, 1)
    raise ValueError(f"unknown tests.parse {spec.parse!r}")


def parse_metric(bench: Benchmark, res: ExecResult) -> float | None:
    if not res.ok:
        return None
    if bench.parse.startswith("json:"):
        obj = _last_json(res.output)
        v = obj.get(bench.parse[5:]) if obj else None
        return float(v) if isinstance(v, int | float) else None
    if bench.parse == "float":
        nums = _NUMBER.findall(res.output)
        return float(nums[-1]) if nums else None
    raise ValueError(f"unknown benchmark parse {bench.parse!r}")


def _clip(s: str, limit: int = RAW_LIMIT) -> str:
    return s if len(s) <= limit else s[: limit // 2] + "\n...[truncated]...\n" + s[-limit // 2 :]
