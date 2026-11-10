"""The "repro" selector: a writer agent's reproduction checks rank branches right after the gates.

Scripted model, real shell (LocalBackend), the count_primes demo task. The fast branch is wrong
for one input the visible tests don't cover, so the default selector picks it (benchmarks); the
writer's checks catch it and the repro selector picks the slower, correct branch.
"""

import asyncio

from test_explore import SIEVE, SLOW_SIEVE, demo_task, make

from sfork.agent.llm import text, tool_use
from sfork.eval.repro import ReproResult, parse
from sfork.eval.scoring import Signal

FAST_BUT_WRONG = SIEVE.replace("return sum(s)", "return sum(s) + (1 if n == 12345 else 0)")
IMPLS = {"fastwrong": FAST_BUT_WRONG, "slowright": SLOW_SIEVE}
ORACLE = "from primes import count_primes; assert count_primes(12345) == 1474"
REPRO_DIR = "/workspace/.git/sfork_repro"  # LocalBackend shares /tmp between "sandboxes"

RUN_SH = """\
check() {
  if timeout 60 python3 -c "$2" >/dev/null 2>&1; then echo "REPRO $1 PASS"; else echo "REPRO $1 FAIL"; fi
}
check primes_12345 'from primes import count_primes; assert count_primes(12345) == 1474'
check primes_10 'from primes import count_primes; assert count_primes(10) == 4'
check module_imports 'import primes'
"""


def is_writer(messages):
    first = messages[0]["content"]
    return isinstance(first, str) and first.startswith("Here is the task the other engineers")


def script(branch, turn, messages):
    if is_writer(messages):
        if turn == 0:
            return [
                tool_use("write_file", {"path": f"{REPRO_DIR}/run.sh", "content": RUN_SH}),
                tool_use("bash", {"command": f"bash {REPRO_DIR}/run.sh"}),
            ]
        return [tool_use("done", {"summary": "3 checks"})]
    impl = IMPLS[branch]
    if turn == 0:
        return [
            text(f"Implementing {branch}."),
            tool_use("write_file", {"path": "primes.py", "content": impl}),
        ]
    return [tool_use("done", {"summary": f"{branch} implemented"})]


ALTS = [{"name": "fastwrong", "plan": "fast"}, {"name": "slowright", "plan": "slow"}]


def test_repro_selector_overrides_benchmarks_and_records_default_pick():
    explorer, backend, llm, events = make(
        script,
        initial_alternatives=ALTS,
        selector="repro",
        repro_dir=REPRO_DIR,
        branch_checks={"oracle": f"python3 -c '{ORACLE}'"},
    )
    report = asyncio.run(explorer.run(demo_task()))
    assert report["status"] == "ok"
    fp = report["fork_points"][0]
    assert fp["selector"] == "repro"
    assert fp["default_selection"]["winner"] == "fastwrong"  # what the old selector picks
    assert report["head_branch"] == "slowright"
    assert fp["deciding"] == "repro" and "3/3 vs 2/3" in fp["why"]
    repro = fp["repro"]
    assert repro["ok"] and repro["checks"] == ["primes_12345", "primes_10", "module_imports"]
    # the fork point is the unimplemented stub: the behaviour checks fail there
    assert repro["fork_point"]["primes_12345"] is False
    by_label = {b["label"]: b for b in repro["branches"].values()}
    assert by_label["slowright"]["passed"] == 3 and by_label["fastwrong"]["passed"] == 2
    # the oracle ran in both branches and is only recorded
    oracle = {e["label"]: e["branch_checks"]["oracle"]["exit_code"] for e in fp["evaluations"]}
    assert oracle == {"fastwrong": 1, "slowright": 0}
    # the writer is metered apart from the branches
    assert report["selection_cost"]["calls"] == 2 and report["cost"]["calls"] == 4
    assert backend.live_ids() == set()
    assert any(e["type"] == "repro_written" and e["ok"] for e in events)


def test_repro_selector_falls_back_when_the_writer_leaves_nothing():
    def lazy(branch, turn, messages):
        if is_writer(messages):
            return [tool_use("done", {"summary": "nothing"})]
        return script(branch, turn, messages)

    explorer, backend, _, _ = make(lazy, initial_alternatives=ALTS, selector="repro", repro_dir=REPRO_DIR)
    report = asyncio.run(explorer.run(demo_task()))
    fp = report["fork_points"][0]
    assert not fp["repro"]["ok"] and "run.sh" in fp["repro"]["reason"]
    assert report["head_branch"] == "fastwrong" == fp["default_selection"]["winner"]
    assert backend.live_ids() == set()


def test_parse_and_signal():
    out = "noise\nREPRO a PASS\nREPRO b FAIL\nREPRO a FAIL\n  REPRO c PASS trailing\n"
    assert parse(out) == {"a": False, "b": False, "c": True}
    r = ReproResult(ok=True, fork_point={"a": False, "b": True})
    r.branches = {"n1": {"a": True, "b": True, "c": True}, "n2": {"b": True}}
    assert r.checks == ["a", "b", "c"]
    sig = r.signal()
    assert isinstance(sig, Signal)
    assert sig.shown == {"n1": "3/3", "n2": "1/3"}
    assert ReproResult(ok=False).signal() is None
