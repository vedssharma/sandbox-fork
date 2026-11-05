"""Evaluator config (PLAN.md section 6), loaded from `sandbox-fork.toml`.

workdir = "/workspace"
setup = ["pip install -r requirements.txt"]

[[gates]]                   # must exit 0, or the branch is disqualified
name = "compiles"
cmd = "python3 -m py_compile primes.py"

[tests]
cmd = "python3 test_primes.py"
parse = "json"              # json: last JSON line with "passed" and "total"
                            # pytest: "N passed, M failed" summary | exit: 1/1 if exit 0 else 0/1
timeout = 300

[[benchmarks]]
name = "latency_ms"
cmd = "python3 bench.py"
parse = "json:ms"           # json:<key> from the last JSON line, or "float" (last number printed)
direction = "lower"
repeats = 3                 # median of repeats
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Gate:
    name: str
    cmd: str
    timeout: int = 300


@dataclass(frozen=True)
class TestSpec:
    __test__ = False  # not a pytest class

    cmd: str
    parse: str = "json"
    timeout: int = 600


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
class EvalConfig:
    workdir: str = "/workspace"
    setup: tuple[str, ...] = ()
    gates: tuple[Gate, ...] = ()
    tests: TestSpec | None = None
    benchmarks: tuple[Benchmark, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvalConfig:
        tests = d.get("tests")
        return cls(
            workdir=d.get("workdir", "/workspace"),
            setup=tuple(d.get("setup", ())),
            gates=tuple(Gate(**g) for g in d.get("gates", ())),
            tests=TestSpec(**tests) if tests else None,
            benchmarks=tuple(Benchmark(**b) for b in d.get("benchmarks", ())),
        )

    @classmethod
    def load(cls, path: str | Path) -> EvalConfig:
        with open(path, "rb") as f:
            return cls.from_dict(tomllib.load(f))


__all__ = ["Benchmark", "EvalConfig", "Gate", "TestSpec"]
