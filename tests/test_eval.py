import asyncio
import json

import pytest

from sfork.agent.llm import ScriptedLLM, text, tool_use
from sfork.daytona.base import ExecResult
from sfork.eval import (
    ArtifactStore,
    Benchmark,
    EvalConfig,
    Evaluation,
    Gate,
    Judge,
    Lint,
    Quality,
    TestSpec,
    evaluate,
    load_raw,
    parse_junit,
    rank,
    select_winner,
)
from sfork.eval.evaluator import match_any, parse_metric, parse_tests

BENCH = (Benchmark(name="ms", cmd="x", direction="lower"),)
CFG = EvalConfig(tests=TestSpec("t"), benchmarks=BENCH)


def ev(label, passed=10, total=10, ms=None, gates_ok=True, tests=None, files=None):
    e = Evaluation(node_id=label, label=label, passed=passed, total=total, gates={"build": gates_ok})
    e.metrics["ms"] = ms
    e.tests = tests or {}
    e.files_changed = files
    return e


def test_rank_is_correctness_first():
    ranked = rank(
        [ev("fast-wrong", passed=9, ms=1), ev("slow-right", ms=1000), ev("broken", ms=0.5, gates_ok=False)],
        CFG,
    )
    assert [e.label for e in ranked] == ["slow-right", "fast-wrong", "broken"]
    assert ranked[2].score == 0
    sel = asyncio.run(select_winner(ranked, CFG))
    assert sel.deciding == "tests" and "more tests pass (10/10 vs 9/10" in sel.why


def test_weighted_score_without_correctness_first_lets_speed_win():
    cfg = EvalConfig(
        tests=TestSpec("t"), benchmarks=BENCH, correctness_first=False, weights={"tests": 0.1, "perf": 0.9}
    )
    ranked = rank([ev("slow-right", ms=1000), ev("fast-nearly", passed=9, ms=1)], cfg)
    assert ranked[0].label == "fast-nearly"


def test_perf_is_relative_to_the_fork_point_when_it_has_a_number():
    base = ev("base", passed=10, ms=100)
    evals = [ev("a", ms=200), ev("b", ms=50), ev("c", ms=None)]
    for _ in range(2):  # scoring is idempotent
        ranked = rank(evals, CFG, base)
    assert [e.label for e in ranked] == ["b", "a", "c"]
    b, a, c = ranked
    assert b.components["perf"] == pytest.approx(2 / 3)  # 2x better than the fork point
    assert a.components["perf"] == pytest.approx(1 / 3)  # 2x worse
    assert c.components["perf"] == 0
    assert "2x better" in b.baseline_deltas["ms"] and "2x worse" in a.baseline_deltas["ms"]


def test_perf_falls_back_to_best_branch_without_a_baseline_number():
    ranked = rank([ev("a", ms=200), ev("b", ms=100)], CFG, ev("base", passed=0, ms=None))
    assert ranked[0].label == "b" and ranked[0].components["perf"] == pytest.approx(0.5)
    assert ranked[1].components["perf"] == pytest.approx(1 / 3)


def test_higher_is_better_direction():
    cfg = EvalConfig(benchmarks=(Benchmark(name="ms", cmd="x", direction="higher"),))
    assert rank([ev("a", ms=5), ev("b", ms=10)], cfg)[0].label == "b"


def test_regressions_against_the_fork_point_disqualify_by_default():
    base = ev("base", passed=2, total=3, tests={"t1": True, "t2": True, "t3": False})
    fixes_t3_breaks_t1 = ev("x", passed=2, total=3, tests={"t1": False, "t2": True, "t3": True})
    keeps = ev("y", passed=2, total=3, tests={"t1": True, "t2": True, "t3": False})
    ranked = rank([fixes_t3_breaks_t1, keeps], CFG, base)
    assert ranked[0].label == "y"
    assert ranked[1].regressions == ["t1"] and not ranked[1].gates_ok
    assert "passed at the fork point" in ranked[1].disqualified[0]

    pen = EvalConfig(tests=TestSpec("t", regressions="penalty"))
    ranked = rank([fixes_t3_breaks_t1, keeps], pen, base)
    assert all(e.gates_ok for e in ranked)
    assert fixes_t3_breaks_t1.components["tests"] == pytest.approx(2 / 3 - 1 / 3)


def test_protected_paths_disqualify_and_diff_size_prefers_small_focused_changes():
    cfg = EvalConfig(
        tests=TestSpec("t"),
        quality=Quality(protected=("tests/**",), expected=("app/**", "migrations/**")),
    )
    small = ev("small", files={"migrations/0003.sql": (3, 1)})
    big = ev("big", files={"migrations/0003.sql": (30, 1), "app/billing.py": (10, 5)})
    stray = ev("stray", files={"migrations/0003.sql": (3, 1), "notes.txt": (1, 0)})
    cheat = ev("cheat", files={"migrations/0003.sql": (3, 1), "tests/test_billing.py": (1, 1)})
    ranked = rank([big, cheat, stray, small], cfg)
    assert [e.label for e in ranked] == ["small", "stray", "big", "cheat"]
    assert cheat.protected_touched == ["tests/test_billing.py"] and not cheat.gates_ok
    assert stray.outside_expected == ["notes.txt"]
    assert stray.components["diff_size"] < small.components["diff_size"] == 1.0
    sel = asyncio.run(select_winner([big, small], cfg))
    assert sel.deciding == "diff_size" and "smaller change (4 vs 46 lines" in sel.why


def test_lint_counts_new_findings_since_the_fork_point():
    cfg = EvalConfig(quality=Quality(lint=Lint("ruff")), weights={"lint": 1.0})
    base = ev("base")
    base.lint_findings = 3
    a, b = ev("a"), ev("b")
    a.lint_findings, b.lint_findings = 3, 5
    ranked = rank([b, a], cfg, base)
    assert (
        ranked[0].label == "a" and a.components["lint"] == 1 and b.components["lint"] == pytest.approx(1 / 3)
    )


def test_match_any_globs():
    assert match_any("tests/test_x.py", ["tests/**"])
    assert match_any("a/b/__pycache__/x.pyc", ["**/__pycache__/**"])
    assert match_any("__pycache__/x.pyc", ["**/__pycache__/**"])
    assert match_any("bench.py", ["bench.py"]) and not match_any("app/bench.py", ["bench.py"])


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_b" name="t_ok" time="0.01"/>
  <testcase classname="tests.test_b" name="t_fail"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="tests.test_b" name="t_err"><error message="boom"/></testcase>
  <testcase classname="tests.test_b" name="t_skip"><skipped message="later"/></testcase>
  <testcase classname="tests.test_b" name="t_ok"/>
</testsuite></testsuites>"""


def test_parse_junit():
    cases = parse_junit(JUNIT)
    assert [(c.name.split("::")[1], c.status) for c in cases] == [
        ("t_ok", "passed"),
        ("t_fail", "failed"),
        ("t_err", "error"),
        ("t_skip", "skipped"),
        ("t_ok#2", "passed"),
    ]
    assert cases[1].message == "assert 1 == 2"
    with pytest.raises(ValueError):
        parse_junit("<html/>")
    with pytest.raises(ValueError):
        parse_junit("not xml")


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


def fake_exec(outputs, calls):
    async def exec_(cmd, timeout):
        calls.append(cmd)
        key = cmd.split("&& ", 1)[1] if cmd.startswith("cd ") else cmd
        out = outputs[next(k for k in outputs if key.startswith(k) or key.endswith(k))]
        return out.pop(0) if isinstance(out, list) else out

    return exec_


def test_evaluate_runs_gates_tests_and_median_benchmark():
    calls = []
    outputs = {
        "build": ExecResult(0, "ok"),
        "test": ExecResult(0, '{"passed": 2, "total": 2}'),
        "bench": [ExecResult(0, "30"), ExecResult(0, "10"), ExecResult(0, "20")],
    }
    cfg = EvalConfig(
        workdir="/w",
        gates=(Gate("build", "build"),),
        tests=TestSpec("test"),
        benchmarks=(Benchmark("ms", "bench", repeats=3),),
        quality=Quality(measure_diff=False),
    )
    e = asyncio.run(evaluate(fake_exec(outputs, calls), cfg, node_id="n"))
    assert e.gates_ok and (e.passed, e.total) == (2, 2) and e.metrics["ms"] == 20
    assert e.metric_runs["ms"] == [30, 10, 20]
    assert all(c.startswith("cd /w && ") for c in calls)
    assert set(e.artifacts) == {
        "gate-build.log",
        "tests.log",
        "bench-ms-0.log",
        "bench-ms-1.log",
        "bench-ms-2.log",
    }


def test_evaluate_reads_junit_with_repeats_and_flags_flaky_tests():
    run1 = JUNIT
    run2 = JUNIT.replace('<failure message="assert 1 == 2">trace</failure>', "")  # t_fail passes once
    run3 = JUNIT
    calls = []
    outputs = {
        "rm -f /tmp/j.xml; pytest": ExecResult(1, "3 failed"),
        "cat /tmp/j.xml": [ExecResult(0, run1), ExecResult(0, run2), ExecResult(0, run3)],
    }
    cfg = EvalConfig(
        tests=TestSpec("pytest", parse="junit", report="junit:/tmp/j.xml", repeats=3),
        quality=Quality(measure_diff=False),
    )
    e = asyncio.run(evaluate(fake_exec(outputs, calls), cfg, node_id="n"))
    assert (e.passed, e.total) == (2, 4)  # t_ok, t_ok#2 pass; t_fail fails 2/3; t_err; t_skip left out
    assert e.tests["tests.test_b::t_fail"] is False and e.flaky == ["tests.test_b::t_fail"]
    assert {"junit-0.xml", "junit-1.xml", "junit-2.xml"} <= set(e.artifacts)


def test_evaluate_measures_the_diff_against_the_baseline_tag():
    numstat = "3\t1\tmigrations/0003.sql\n-\t-\tlogo.png\n"
    outputs = {
        "t": ExecResult(0, '{"passed": 1, "total": 1}'),
        'git diff --cached "$base"': ExecResult(0, numstat + "---sfork-patch---\ndiff --git a/x b/x\n"),
    }

    async def exec_(cmd, timeout):
        if "---sfork-patch---" in cmd:
            assert "sfork-base" in cmd and ":(exclude,glob)**/__pycache__/**" in cmd
            return outputs['git diff --cached "$base"']
        return outputs["t"]

    e = asyncio.run(evaluate(exec_, EvalConfig(tests=TestSpec("t")), node_id="n"))
    assert e.files_changed == {"migrations/0003.sql": (3, 1), "logo.png": (1, 1)}
    assert e.diff_lines == 6 and e.diff.startswith("diff --git")


def test_evaluate_turns_backend_errors_into_a_losing_evaluation():
    async def exec_(cmd, timeout):
        raise RuntimeError("sandbox gone")

    e = asyncio.run(evaluate(exec_, EvalConfig(tests=TestSpec("t")), node_id="n"))
    assert not e.gates_ok and "sandbox gone" in e.error


# --- judge -------------------------------------------------------------------------------------


def tie_pair():
    cfg = EvalConfig(tests=TestSpec("t"), judge=Judge(enabled=True, margin=0.1))
    a = ev("plain", files={"m.sql": (3, 1)})
    b = ev("transactional", files={"m.sql": (6, 1)})
    a.artifacts["diff.patch"] = "+UPDATE invoices SET ..."
    b.artifacts["diff.patch"] = "+BEGIN;\n+UPDATE invoices SET ...\n+COMMIT;"
    return cfg, a, b


def judge_llm(pick):
    """A judge that always prefers the candidate whose diff contains `pick`."""

    def script(branch, turn, messages):
        prompt = messages[0]["content"]
        a = prompt.split('<candidate id="A">')[1].split("</candidate>")[0]
        return [
            tool_use("verdict", {"better": "A" if pick(a) else "B", "reasons": "wraps it in a transaction"})
        ]

    return ScriptedLLM(script)


def test_judge_breaks_a_near_tie_only_when_consistent_across_orders():
    cfg, a, b = tie_pair()
    llm = judge_llm(lambda diff: "BEGIN" in diff)
    sel = asyncio.run(select_winner([a, b], cfg, llm=llm, model="m", task="make the deploy safe"))
    assert len(llm.calls) == 2  # asked with A/B swapped
    assert sel.winner.label == "transactional" and sel.deciding == "judge"
    assert sel.verdict.consistent and "transaction" in sel.why

    # a position-biased judge (always says A) is inconsistent and ignored
    cfg, a, b = tie_pair()
    biased = ScriptedLLM(lambda br, t, m: [tool_use("verdict", {"better": "A", "reasons": "first"})])
    sel = asyncio.run(select_winner([a, b], cfg, llm=biased, model="m", task="t"))
    assert sel.winner.label == "plain" and not sel.verdict.applied
    assert "ignored" in sel.why


def test_judge_is_not_called_outside_the_margin_or_when_disabled():
    cfg, a, b = tie_pair()
    b.files_changed = {"m.sql": (300, 1)}  # far apart now
    llm = judge_llm(lambda d: True)
    sel = asyncio.run(select_winner([a, b], cfg, llm=llm, model="m"))
    assert llm.calls == [] and sel.verdict is None

    cfg, a, b = tie_pair()
    cfg = EvalConfig(tests=TestSpec("t"))  # judge disabled by default
    asyncio.run(select_winner([a, b], cfg, llm=llm, model="m"))
    assert llm.calls == []


def test_judge_failure_keeps_the_deterministic_order():
    cfg, a, b = tie_pair()

    class Broken:
        async def complete(self, **kw):
            raise RuntimeError("overloaded")

    sel = asyncio.run(select_winner([a, b], cfg, llm=Broken(), model="m"))
    assert sel.winner.label == "plain" and not sel.verdict.applied
    assert "overloaded" in sel.verdict.calls[0]["raw"]


def test_judge_answer_in_text_is_not_a_verdict():
    cfg, a, b = tie_pair()
    llm = ScriptedLLM(lambda br, t, m: [text("I think B")])
    sel = asyncio.run(select_winner([a, b], cfg, llm=llm, model="m"))
    assert sel.winner.label == "plain" and not sel.verdict.applied


# --- config and artifacts ----------------------------------------------------------------------


def test_yaml_config_from_the_plan(tmp_path):
    (tmp_path / "sandbox-fork.yaml").write_text(
        """
setup: ["make deps"]
gates:
  - name: build
    cmd: make build
tests:
  cmd: pytest -q --junitxml=/tmp/junit.xml
  report: junit:/tmp/junit.xml
benchmarks:
  - name: p95_latency_ms
    cmd: ./bench.sh
    parse: json:p95
    direction: lower
quality:
  protected: ["tests/**"]
  lint: ruff check -q .
weights: { tests: 0.6, perf: 0.2, diff_size: 0.1, lint: 0.1 }
judge: { enabled: true, on: tie }
alternatives: [{name: a, plan: b}]
"""
    )
    cfg = EvalConfig.from_dict(load_raw(tmp_path))
    assert cfg.tests.junit_path == "/tmp/junit.xml" and cfg.judge.enabled and cfg.judge.margin == 0.05
    assert cfg.quality.lint.cmd == "ruff check -q ." and cfg.quality.protected == ("tests/**",)
    assert cfg.benchmarks[0].parse == "json:p95"
    with pytest.raises(ValueError):
        EvalConfig.from_dict({"weights": {"speed": 1}})
    with pytest.raises(ValueError):
        EvalConfig.from_dict({"tests": {"cmd": "x", "parse": "junit"}})


def test_artifact_store_writes_everything_for_audit(tmp_path):
    cfg, a, b = tie_pair()
    a.artifacts["junit.xml"] = JUNIT
    sel = asyncio.run(select_winner([a, b], cfg))
    d = ArtifactStore(tmp_path).save_selection("run1", "fork1-root", sel)
    assert (d / "selection.json").exists()
    assert (d / "plain" / "junit.xml").read_text() == JUNIT
    assert (d / "transactional" / "diff.patch").read_text().startswith("+BEGIN")
    assert a.artifact_dir == str(d / "plain")
    data = json.loads((d / "selection.json").read_text())
    assert data["winner"] == "plain" and data["ranking"][0]["qualified"] is True
