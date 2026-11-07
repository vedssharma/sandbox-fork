"""`sfork evaluate` end to end on the count_primes demo: real shell (LocalBackend), no network."""

import asyncio
import dataclasses
import json
from pathlib import Path

from sfork.core.budget import Budget
from sfork.core.store import TreeStore
from sfork.daytona.local import LocalBackend
from sfork.eval.config import Benchmark
from sfork.orchestrator.candidates import Candidate, evaluate_candidates
from sfork.orchestrator.engine import TreeEngine
from sfork.orchestrator.explore import Task

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def count_primes_task() -> Task:
    task = Task.load(EXAMPLES / "count_primes")
    fast = Benchmark(
        name="latency_ms", cmd="SFORK_BENCH_N=50000 python3 bench.py", parse="json:ms", repeats=1
    )
    return dataclasses.replace(task, eval=dataclasses.replace(task.eval, benchmarks=(fast,)))


def test_candidates_pick_the_sieve_and_explain_why(tmp_path):
    backend = LocalBackend()
    engine = TreeEngine(
        TreeStore(), backend, budget=Budget(max_running_sandboxes=8), run_post_fork_hook=False
    )
    cands = Candidate.load_dir(EXAMPLES / "count_primes" / "candidates")
    assert [c.name for c in cands] == ["sieve", "sieve-off-by-one", "sieve-shrinks-bench", "trial-division"]
    events = []
    rep = asyncio.run(
        evaluate_candidates(
            engine, count_primes_task(), cands, artifacts_dir=tmp_path, on_event=events.append
        )
    )
    assert rep["status"] == "ok" and rep["winner"] == "sieve"
    rows = {r["label"]: r for r in rep["selection"]["ranking"]}
    assert not rows["sieve-shrinks-bench"]["qualified"]  # edited bench.py
    assert rows["sieve-off-by-one"]["qualified"]  # wrong, but nothing it broke passed before
    assert [r["label"] for r in rep["selection"]["ranking"]][:3] == [
        "sieve",
        "trial-division",
        "sieve-off-by-one",
    ]
    assert rep["deciding"] == "perf" and "benchmarks (latency_ms" in rep["why"]
    evs = {e["label"]: e for e in rep["evaluations"]}
    assert evs["sieve-shrinks-bench"]["protected_touched"] == ["bench.py"]
    assert evs["sieve"]["files_changed"] == {"primes.py": evs["sieve"]["files_changed"]["primes.py"]}
    assert rep["baseline"]["passed"] == 0 and rep["baseline"]["metrics"]["latency_ms"] is None
    # everything is on disk for audit, and nothing is left running
    d = Path(rep["artifacts"])
    sel = json.loads((d / "selection.json").read_text())
    assert sel["winner"] == "sieve"
    assert {"tests.log", "diff.patch", "apply.log", "evaluation.json"} <= {
        p.name for p in (d / "sieve").iterdir()
    }
    assert (d / "baseline" / "tests.log").exists()
    assert "count_primes" in rep["winner_diff"]
    assert backend.live_ids() == set() and rep["leftover_sandboxes"] == []
