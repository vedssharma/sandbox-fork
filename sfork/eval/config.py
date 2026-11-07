"""Evaluator config (PLAN.md section 6), loaded from `sandbox-fork.yaml` (or `.yml` / `.toml`).

```yaml
workdir: /workspace
setup: ["pip install -r requirements.txt"]     # run once in the root, before anything forks
gates:                                         # must exit 0, or the branch is disqualified
  - {name: compiles, cmd: "python3 -m py_compile app.py", timeout: 60}
tests:
  cmd: "pytest -q --junitxml=/tmp/junit.xml"
  report: junit:/tmp/junit.xml   # per-test results; or parse: json | pytest | exit (counts only)
  repeats: 1                     # >1 for flaky suites: per-test majority, median pass count
  regressions: gate              # a test that passed at the fork point and fails now:
                                 #   gate (disqualify) | penalty (subtract) | ignore
benchmarks:
  - {name: latency_ms, cmd: "python3 bench.py", parse: "json:ms", direction: lower, repeats: 3}
quality:
  protected: ["tests/**"]        # touching these disqualifies the branch
  expected: ["app/**", "migrations/**"]   # files outside these count against change quality
  exclude: ["**/__pycache__/**"] # ignored when measuring the diff
  lint: {cmd: "ruff check -q .", parse: lines}   # findings counted relative to the fork point
weights: {tests: 0.6, perf: 0.2, diff_size: 0.1, lint: 0.1}   # components that don't apply drop out
correctness_first: true          # a branch passing fewer tests never outranks one passing more
judge: {enabled: true, margin: 0.05}  # LLM judge only breaks near-ties between the top two
baseline: true                   # evaluate the fork point too, and score relative to it
```

Unknown top-level keys (`alternatives`, `checkpoints`) belong to other parts of sfork and are ignored
here. The TOML form uses the same keys (`[[gates]]`, `[tests]`, `[[benchmarks]]`, ...).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAMES = ("sandbox-fork.yaml", "sandbox-fork.yml", "sandbox-fork.toml")
DEFAULT_WEIGHTS = {"tests": 0.6, "perf": 0.2, "diff_size": 0.1, "lint": 0.1}
DEFAULT_EXCLUDE = ("**/__pycache__/**", "*.pyc", ".pytest_cache/**")


@dataclass(frozen=True)
class Gate:
    name: str
    cmd: str
    timeout: int = 300


@dataclass(frozen=True)
class TestSpec:
    __test__ = False  # not a pytest class

    cmd: str
    parse: str = "json"  # json | pytest | exit | junit (needs `report`)
    timeout: int = 600
    report: str | None = None  # "junit:/path/in/sandbox.xml"
    repeats: int = 1
    regressions: str = "gate"

    def __post_init__(self) -> None:
        if self.report is not None and not self.report.startswith("junit:"):
            raise ValueError(f"tests.report must look like 'junit:/path.xml', got {self.report!r}")
        if self.parse == "junit" and self.report is None:
            raise ValueError("tests.parse = junit needs tests.report = 'junit:/path.xml'")
        if self.regressions not in ("gate", "penalty", "ignore"):
            raise ValueError("tests.regressions must be gate, penalty or ignore")

    @property
    def junit_path(self) -> str | None:
        return self.report[len("junit:") :] if self.report else None


@dataclass(frozen=True)
class Benchmark:
    name: str
    cmd: str
    parse: str = "float"
    direction: str = "lower"
    repeats: int = 1
    timeout: int = 600

    def __post_init__(self) -> None:
        if self.direction not in ("lower", "higher"):
            raise ValueError(f"benchmark {self.name}: direction must be 'lower' or 'higher'")


@dataclass(frozen=True)
class Lint:
    cmd: str
    parse: str = "lines"  # lines (non-empty output lines) | json:<key> | float (last number) | exit
    timeout: int = 300


@dataclass(frozen=True)
class Quality:
    protected: tuple[str, ...] = ()
    expected: tuple[str, ...] = ()
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    lint: Lint | None = None
    measure_diff: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Quality:
        d = dict(d or {})
        lint = d.pop("lint", None)
        return cls(
            protected=tuple(d.pop("protected", ())),
            expected=tuple(d.pop("expected", ())),
            exclude=tuple(d.pop("exclude", DEFAULT_EXCLUDE)),
            lint=Lint(**lint) if isinstance(lint, dict) else Lint(cmd=lint) if lint else None,
            measure_diff=bool(d.pop("measure_diff", True)),
            **_no_extra("quality", d),
        )


@dataclass(frozen=True)
class Judge:
    enabled: bool = False
    margin: float = 0.05  # top two within this many score points (0..1) count as a near-tie
    model: str | None = None  # default: the run's model
    both_orders: bool = True  # ask twice with A/B swapped; only a consistent verdict reorders
    max_diff_chars: int = 30_000

    @classmethod
    def from_dict(cls, d: dict[str, Any] | bool | None) -> Judge:
        if isinstance(d, bool) or d is None:
            return cls(enabled=bool(d))
        d = dict(d)
        # PLAN.md spells it `on: tie`; near-ties are the only mode. (YAML 1.1 reads a bare `on` as True.)
        d.pop("on", None)
        d.pop(True, None)
        return cls(**d)


@dataclass(frozen=True)
class EvalConfig:
    workdir: str = "/workspace"
    setup: tuple[str, ...] = ()
    gates: tuple[Gate, ...] = ()
    tests: TestSpec | None = None
    benchmarks: tuple[Benchmark, ...] = ()
    quality: Quality = field(default_factory=Quality)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    correctness_first: bool = True
    judge: Judge = field(default_factory=Judge)
    baseline: bool = True

    def __post_init__(self) -> None:
        bad = set(self.weights) - set(DEFAULT_WEIGHTS)
        if bad:
            raise ValueError(f"unknown weights {sorted(bad)}; use {sorted(DEFAULT_WEIGHTS)}")
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("weights must be >= 0")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvalConfig:
        tests = d.get("tests")
        weights = dict(DEFAULT_WEIGHTS)
        if d.get("weights"):
            weights = {k: float(v) for k, v in d["weights"].items()}
        return cls(
            workdir=d.get("workdir", "/workspace"),
            setup=tuple(d.get("setup", ())),
            gates=tuple(Gate(**g) for g in d.get("gates", ())),
            tests=TestSpec(**tests) if tests else None,
            benchmarks=tuple(Benchmark(**b) for b in d.get("benchmarks", ())),
            quality=Quality.from_dict(d.get("quality")),
            weights=weights,
            correctness_first=bool(d.get("correctness_first", True)),
            judge=Judge.from_dict(d.get("judge")),
            baseline=bool(d.get("baseline", True)),
        )

    @classmethod
    def load(cls, path: str | Path) -> EvalConfig:
        return cls.from_dict(load_raw(path))


def find_config(task_dir: str | Path) -> Path:
    d = Path(task_dir)
    for name in CONFIG_NAMES:
        if (d / name).is_file():
            return d / name
    raise FileNotFoundError(f"no {' / '.join(CONFIG_NAMES)} in {d}")


def load_raw(path: str | Path) -> dict[str, Any]:
    """Read a config file (YAML or TOML by extension); a directory means its sandbox-fork.* file."""
    p = Path(path)
    if p.is_dir():
        p = find_config(p)
    if p.suffix == ".toml":
        with open(p, "rb") as f:
            return tomllib.load(f)
    import yaml

    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping at the top level")
    return data


def _no_extra(section: str, d: dict[str, Any]) -> dict[str, Any]:
    if d:
        raise ValueError(f"unknown {section} keys: {sorted(d)}")
    return {}


__all__ = [
    "Benchmark",
    "EvalConfig",
    "Gate",
    "Judge",
    "Lint",
    "Quality",
    "TestSpec",
    "find_config",
    "load_raw",
]
