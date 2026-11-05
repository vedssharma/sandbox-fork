import asyncio

from sfork.daytona.base import ExecResult
from sfork.eval import Benchmark, EvalConfig, Evaluation, Gate, TestSpec, evaluate, explain, rank
from sfork.eval.evaluator import parse_metric, parse_tests


def ev(label, passed=10, total=10, ms=None, gates_ok=True):
    e = Evaluation(node_id=label, label=label, passed=passed, total=total, gates={"build": gates_ok})
    e.metrics["ms"] = ms
    return e


BENCH = [Benchmark(name="ms", cmd="x", direction="lower")]


def test_rank_is_lexicographic_correctness_before_speed():
    ranked = rank(
        [ev("fast-wrong", passed=9, ms=1), ev("slow-right", ms=1000), ev("broken", ms=0.5, gates_ok=False)],
        BENCH,
    )
    assert [e.label for e in ranked] == ["slow-right", "fast-wrong", "broken"]
    assert "higher test pass rate" in explain(ranked)


def test_rank_uses_benchmarks_among_equals_and_is_repeatable():
    evals = [ev("a", ms=200), ev("b", ms=100), ev("c", ms=None)]
    for _ in range(2):  # rank resets perf scores, so calling it twice gives the same answer
        ranked = rank(evals, BENCH)
    assert [e.label for e in ranked] == ["b", "a", "c"]
    assert ranked[0].perf_score == 1.0 and ranked[1].perf_score == 0.5 and ranked[2].perf_score == 0.0


def test_higher_is_better_direction():
    hb = [Benchmark(name="ms", cmd="x", direction="higher")]
    assert rank([ev("a", ms=5), ev("b", ms=10)], hb)[0].label == "b"


def test_parsers():
    assert parse_tests(TestSpec("x", "json"), ExecResult(1, 'noise\n{"passed": 3, "total": 4}\n')) == (3, 4)
    assert parse_tests(TestSpec("x", "json"), ExecResult(1, "Traceback...")) == (0, 1)
    assert parse_tests(
        TestSpec("x", "pytest"), ExecResult(1, "== 5 passed, 2 failed, 1 error in 0.1s ==")
    ) == (5, 8)
    assert parse_tests(TestSpec("x", "exit"), ExecResult(0, "")) == (1, 1)
    assert parse_metric(Benchmark("m", "x", parse="json:ms"), ExecResult(0, '{"ms": 12.5}')) == 12.5
    assert parse_metric(Benchmark("m", "x", parse="float"), ExecResult(0, "took 3.25 ms\n")) == 3.25
    assert parse_metric(Benchmark("m", "x", parse="float"), ExecResult(1, "1.0")) is None


def test_evaluate_runs_gates_tests_and_median_benchmark():
    outputs = {
        "build": ExecResult(0, "ok"),
        "test": ExecResult(0, '{"passed": 2, "total": 2}'),
        "bench": [ExecResult(0, "30"), ExecResult(0, "10"), ExecResult(0, "20")],
    }
    calls = []

    async def exec_(cmd, timeout):
        calls.append(cmd)
        key = cmd.split("&& ", 1)[1]
        out = outputs[key]
        return out.pop(0) if isinstance(out, list) else out

    cfg = EvalConfig(
        workdir="/w",
        gates=(Gate("build", "build"),),
        tests=TestSpec("test"),
        benchmarks=(Benchmark("ms", "bench", repeats=3),),
    )
    e = asyncio.run(evaluate(exec_, cfg, node_id="n"))
    assert e.gates_ok and (e.passed, e.total) == (2, 2) and e.metrics["ms"] == 20
    assert all(c.startswith("cd /w && ") for c in calls)


def test_evaluate_turns_backend_errors_into_a_losing_evaluation():
    async def exec_(cmd, timeout):
        raise RuntimeError("sandbox gone")

    e = asyncio.run(evaluate(exec_, EvalConfig(tests=TestSpec("t")), node_id="n"))
    assert not e.gates_ok and "sandbox gone" in e.error
