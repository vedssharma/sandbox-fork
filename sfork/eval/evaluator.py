"""Run gates, tests, benchmarks and change-quality checks inside one branch's sandbox (PLAN.md 6).

`evaluate` only measures. Scoring and ranking across branches, relative to the fork-point baseline,
live in `sfork.eval.scoring`; the optional LLM judge in `sfork.eval.judge`.

Everything a command prints is kept in full in `Evaluation.artifacts` (junit XML, benchmark output,
the diff) so `sfork.eval.artifacts.ArtifactStore` can store it for audit; `raw` holds clipped copies
for JSON reports.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..daytona.base import ExecResult
from .config import Benchmark, EvalConfig, Lint, TestSpec
from .junit import JUnitError, counts, parse_junit

Exec = Callable[[str, int], Awaitable[ExecResult]]
RAW_LIMIT = 4000
BASE_TAG = "sfork-base"  # the explorer tags the seeded baseline commit with this
_PATCH_MARK = "---sfork-patch---"


@dataclass
class Evaluation:
    node_id: str
    label: str | None = None
    gates: dict[str, bool] = field(default_factory=dict)
    passed: int = 0
    total: int = 0
    tests: dict[str, bool] = field(default_factory=dict)  # per-test outcome, when the runner reports it
    flaky: list[str] = field(default_factory=list)  # tests that both passed and failed across repeats
    test_runs: list[tuple[int, int]] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    metric_runs: dict[str, list[float]] = field(default_factory=dict)
    files_changed: dict[str, tuple[int, int]] | None = None  # path -> (added, deleted); None: no git
    lint_findings: float | None = None
    error: str | None = None
    seconds: float = 0.0
    raw: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)  # full outputs, by artifact file name
    # filled in by scoring.score()
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    regressions: list[str] = field(default_factory=list)
    disqualified: list[str] = field(default_factory=list)
    protected_touched: list[str] = field(default_factory=list)
    outside_expected: list[str] = field(default_factory=list)
    baseline_deltas: dict[str, str] = field(default_factory=dict)
    artifact_dir: str | None = None

    @property
    def gates_ok(self) -> bool:
        return self.error is None and all(self.gates.values()) and not self.disqualified

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def diff_lines(self) -> int | None:
        if self.files_changed is None:
            return None
        return sum(a + d for a, d in self.files_changed.values())

    # kept for callers that still read it; the weighted perf component
    @property
    def perf_score(self) -> float:
        return self.components.get("perf", 0.0)

    def rank_key(self, correctness_first: bool = True) -> tuple[int, float, float]:
        return (
            int(self.gates_ok),
            round(self.pass_rate, 6) if correctness_first else 0.0,
            round(self.score, 6),
        )

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        parts = []
        bad = [g for g, ok in self.gates.items() if not ok]
        if bad:
            parts.append(f"gates failed: {', '.join(bad)}")
        parts.extend(self.disqualified)
        parts.append(f"tests {self.passed}/{self.total}")
        if self.flaky:
            parts.append(f"flaky: {', '.join(self.flaky[:3])}")
        for name, v in self.metrics.items():
            parts.append(f"{name}={'n/a' if v is None else f'{v:.4g}'}")
        if self.diff_lines is not None:
            parts.append(f"diff {self.diff_lines} lines in {len(self.files_changed or {})} files")
        if self.lint_findings is not None:
            parts.append(f"lint {self.lint_findings:g}")
        if self.components:
            parts.append(f"score {self.score:.3f}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "gates": self.gates,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "tests": self.tests,
            "flaky": self.flaky,
            "test_runs": self.test_runs,
            "metrics": self.metrics,
            "metric_runs": self.metric_runs,
            "files_changed": self.files_changed,
            "diff_lines": self.diff_lines,
            "lint_findings": self.lint_findings,
            "components": self.components,
            "score": round(self.score, 6),
            "perf_score": round(self.perf_score, 6),
            "regressions": self.regressions,
            "disqualified": self.disqualified,
            "protected_touched": self.protected_touched,
            "outside_expected": self.outside_expected,
            "baseline_deltas": self.baseline_deltas,
            "error": self.error,
            "seconds": round(self.seconds, 3),
            "artifact_dir": self.artifact_dir,
            "raw": self.raw,
        }

    @property
    def diff(self) -> str:
        return self.artifacts.get("diff.patch", "")


def in_dir(workdir: str, cmd: str) -> str:
    return f"cd {shlex.quote(workdir)} && {cmd}"


async def evaluate(
    exec_: Exec,
    cfg: EvalConfig,
    *,
    node_id: str,
    label: str | None = None,
    quality: bool = True,
) -> Evaluation:
    """Run the eval suite through `exec_` (bound to one sandbox). Never raises for command failures."""
    ev = Evaluation(node_id=node_id, label=label)
    t0 = time.monotonic()
    try:
        for gate in cfg.gates:
            res = await exec_(in_dir(cfg.workdir, gate.cmd), gate.timeout)
            ev.gates[gate.name] = res.ok
            _keep(ev, f"gate-{gate.name}.log", f"gate:{gate.name}", res)
        if cfg.tests is not None:
            await _run_tests(exec_, cfg, cfg.tests, ev)
        for bench in cfg.benchmarks:
            values: list[float] = []
            for i in range(max(1, bench.repeats)):
                res = await exec_(in_dir(cfg.workdir, bench.cmd), bench.timeout)
                _keep(ev, f"bench-{bench.name}-{i}.log", f"bench:{bench.name}:{i}", res)
                v = parse_metric(bench, res)
                if v is None:
                    values = []
                    break
                values.append(v)
            ev.metric_runs[bench.name] = values
            ev.metrics[bench.name] = statistics.median(values) if values else None
        if quality and cfg.quality.measure_diff:
            await _measure_diff(exec_, cfg, ev)
        if quality and cfg.quality.lint is not None:
            lint = cfg.quality.lint
            res = await exec_(in_dir(cfg.workdir, lint.cmd), lint.timeout)
            _keep(ev, "lint.log", "lint", res)
            ev.lint_findings = parse_lint(lint, res)
    except Exception as e:  # sandbox gone, backend error: the branch just loses
        ev.error = f"{type(e).__name__}: {e}"
    ev.seconds = time.monotonic() - t0
    return ev


async def _run_tests(exec_: Exec, cfg: EvalConfig, spec: TestSpec, ev: Evaluation) -> None:
    runs: list[tuple[int, int, dict[str, bool]]] = []
    n = max(1, spec.repeats)
    for i in range(n):
        sfx = f"-{i}" if n > 1 else ""
        cmd = spec.cmd
        jp = spec.junit_path
        if jp:
            cmd = f"rm -f {shlex.quote(jp)}; {cmd}"
        res = await exec_(in_dir(cfg.workdir, cmd), spec.timeout)
        _keep(ev, f"tests{sfx}.log", f"tests{sfx}" if n > 1 else "tests", res)
        per_test: dict[str, bool] = {}
        passed, total = parse_tests(spec, res) if spec.parse != "junit" else (0, 1)
        if jp:
            xml = await exec_(f"cat {shlex.quote(jp)}", 60)
            if xml.ok and xml.output.strip():
                ev.artifacts[f"junit{sfx}.xml"] = xml.output
                try:
                    cases = parse_junit(xml.output)
                    per_test = {c.name: c.ok for c in cases if c.status != "skipped"}
                    passed, total = counts(cases)
                    if total == 0:
                        passed, total = 0, 1
                except JUnitError as e:
                    ev.raw[f"junit{sfx}"] = str(e)
            else:
                ev.raw[f"junit{sfx}"] = "no junit report was written"
        else:
            per_test = _json_results(res.output) if spec.parse == "json" else {}
        runs.append((passed, total, per_test))
    ev.test_runs = [(p, t) for p, t, _ in runs]
    names = sorted({k for _, _, d in runs for k in d})
    if names:
        for name in names:
            outcomes = [d[name] for _, _, d in runs if name in d]
            ev.tests[name] = sum(outcomes) * 2 > len(outcomes)  # strict majority passes
            if len(set(outcomes)) > 1:
                ev.flaky.append(name)
        ev.passed, ev.total = sum(ev.tests.values()), len(ev.tests)
    else:
        ev.passed = int(statistics.median_low([p for p, _, _ in runs]))
        ev.total = max(t for _, t, _ in runs)


async def _measure_diff(exec_: Exec, cfg: EvalConfig, ev: Evaluation) -> None:
    """Diff the working tree (tracked and untracked) against the seeded baseline commit."""
    excludes = " ".join(shlex.quote(f":(exclude,glob){g}") for g in cfg.quality.exclude)
    spec = f"-- . {excludes}"
    git = "git -c core.pager=cat -c core.quotepath=off"
    cmd = (
        "command -v git >/dev/null && git rev-parse -q --verify HEAD >/dev/null || exit 3; "
        f"base=$(git rev-parse -q --verify {BASE_TAG} || git rev-list --max-parents=0 HEAD | tail -1); "
        f"{git} add -A {spec} >/dev/null 2>&1; "
        f'{git} diff --cached --numstat "$base" {spec} && echo {_PATCH_MARK} && '
        f'{git} diff --cached "$base" {spec}'
    )
    res = await exec_(in_dir(cfg.workdir, cmd), 120)
    if not res.ok or _PATCH_MARK not in res.output:
        ev.raw["diff"] = _clip(res.output) or f"exit {res.exit_code}"
        return
    numstat, patch = res.output.split(_PATCH_MARK, 1)
    files: dict[str, tuple[int, int]] = {}
    for line in numstat.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        files[path] = (int(a) if a.isdigit() else 1, int(d) if d.isdigit() else 1)  # binary: 1/1
    ev.files_changed = files
    ev.artifacts["diff.patch"] = patch.lstrip("\n")
    ev.raw["diff"] = _clip(ev.artifacts["diff.patch"])


def match_any(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Glob match where `**/` also matches zero directories and a bare dir glob covers its contents."""
    for g in globs:
        if fnmatch.fnmatchcase(path, g):
            return True
        if g.startswith("**/") and fnmatch.fnmatchcase(path, g[3:]):
            return True
        if g.endswith("/**") and (path == g[:-3] or fnmatch.fnmatchcase(path, g[:-3] + "/*")):
            return True
    return False


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


def _json_results(output: str) -> dict[str, bool]:
    """Optional per-test outcomes in the json line: {"results": {"name": true | "passed" | ...}}."""
    obj = _last_json(output) or {}
    res = obj.get("results")
    if not isinstance(res, dict):
        return {}
    return {str(k): v is True or v in ("passed", "pass", "ok") for k, v in res.items()}


def parse_tests(spec: TestSpec, res: ExecResult) -> tuple[int, int]:
    if spec.parse == "json":
        obj = _last_json(res.output)
        if obj and isinstance(obj.get("passed"), int) and isinstance(obj.get("total"), int):
            return obj["passed"], obj["total"]
        return 0, 1
    if spec.parse == "pytest":
        counts_ = {"passed": 0, "failed": 0, "error": 0}
        for n, kind in _PYTEST.findall(res.output):
            counts_["error" if kind.startswith("error") else kind] += int(n)
        total = sum(counts_.values())
        return (counts_["passed"], total) if total else (0, 1)
    if spec.parse == "exit":
        return (1, 1) if res.ok else (0, 1)
    if spec.parse == "junit":
        return 0, 1  # counted from the report by _run_tests
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


def parse_lint(lint: Lint, res: ExecResult) -> float | None:
    if lint.parse == "lines":
        return float(sum(1 for ln in res.output.splitlines() if ln.strip()))
    if lint.parse == "exit":
        return 0.0 if res.ok else 1.0
    if lint.parse.startswith("json:"):
        obj = _last_json(res.output)
        v = obj.get(lint.parse[5:]) if obj else None
        return float(v) if isinstance(v, int | float) else None
    if lint.parse == "float":
        nums = _NUMBER.findall(res.output)
        return float(nums[-1]) if nums else None
    raise ValueError(f"unknown lint parse {lint.parse!r}")


def _keep(ev: Evaluation, artifact: str, raw_key: str, res: ExecResult) -> None:
    ev.artifacts[artifact] = f"$ exit {res.exit_code}\n{res.output}"
    ev.raw[raw_key] = _clip(res.output)


def _clip(s: str, limit: int = RAW_LIMIT) -> str:
    return s if len(s) <= limit else s[: limit // 2] + "\n...[truncated]...\n" + s[-limit // 2 :]


__all__ = ["BASE_TAG", "Evaluation", "evaluate", "in_dir", "match_any", "parse_metric", "parse_tests"]
