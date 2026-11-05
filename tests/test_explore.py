"""End-to-end tests of the agent fork-evaluate-promote loop: scripted model, real shell (LocalBackend),
real demo task. No network, no credentials."""

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from sfork.agent.llm import AnthropicLLM, ScriptedLLM, block_get, text, tool_use
from sfork.core.budget import Budget
from sfork.core.models import NodeStatus
from sfork.core.store import TreeStore
from sfork.daytona.local import LocalBackend
from sfork.eval.config import Benchmark
from sfork.orchestrator.engine import TreeEngine
from sfork.orchestrator.explore import ExploreConfig, Explorer, Task

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "count_primes"

SIEVE = """
def count_primes(n):
    if n < 2:
        return 0
    s = bytearray([1]) * (n + 1)
    s[0] = s[1] = 0
    for i in range(2, int(n ** 0.5) + 1):
        if s[i]:
            s[i * i :: i] = bytes(len(range(i * i, n + 1, i)))
    return sum(s)
"""
SLOW_SIEVE = "import time\n" + SIEVE.replace("    if n < 2:", "    time.sleep(0.2)\n    if n < 2:")
WRONG = SIEVE.replace("return sum(s)", "return sum(s) + (1 if n > 50 else 0)")
IMPLS = {"sieve": SIEVE, "slow": SLOW_SIEVE, "wrong": WRONG}


def demo_task() -> Task:
    task = Task.load(EXAMPLE)
    fast_bench = Benchmark(
        name="latency_ms", cmd="SFORK_BENCH_N=50000 python3 bench.py", parse="json:ms", repeats=1
    )
    return dataclasses.replace(task, eval=dataclasses.replace(task.eval, benchmarks=(fast_bench,)))


def branch_script(branch, turn):
    """What a forked branch does: write its implementation, test it, finish."""
    impl = IMPLS[branch.split("/")[-1].split("-")[0]]
    if turn == 0:
        return [
            text(f"Implementing {branch}."),
            tool_use("write_file", {"path": "primes.py", "content": impl}),
        ]
    if turn == 1:
        return [tool_use("run_tests", {})]
    return [tool_use("done", {"summary": f"{branch} implemented"})]


def standard_script(branch, turn, messages):
    if branch is None:
        if turn == 0:
            return [tool_use("bash", {"command": "cat primes.py && ls"})]
        alts = [{"name": n, "plan": f"use {n}"} for n in ("wrong", "slow", "sieve")]
        return [text("Forking."), tool_use("fork", {"alternatives": alts})]
    return branch_script(branch, turn)


def make(script, **cfg):
    backend = LocalBackend()
    engine = TreeEngine(
        TreeStore(), backend, budget=Budget(max_running_sandboxes=8), run_post_fork_hook=False
    )
    llm = ScriptedLLM(script)
    events = []
    explorer = Explorer(
        engine, llm, ExploreConfig(max_steps_root=6, max_steps_branch=6, **cfg), on_event=events.append
    )
    return explorer, backend, llm, events


def assert_well_formed(messages):
    """Every tool_use is answered by a tool_result in the very next user message (a forked parent's
    history ends at its fork call; each child answers it)."""
    for i, m in enumerate(messages):
        if m["role"] != "assistant" or i == len(messages) - 1:
            continue
        ids = {block_get(b, "id") for b in m["content"] if block_get(b, "type") == "tool_use"}
        if not ids:
            continue
        nxt = messages[i + 1]
        assert nxt["role"] == "user"
        answered = {b["tool_use_id"] for b in nxt["content"] if b.get("type") == "tool_result"}
        assert ids == answered


def test_agent_forks_and_best_branch_wins():
    explorer, backend, llm, events = make(standard_script)
    report = asyncio.run(explorer.run(demo_task()))

    assert report["status"] == "ok"
    assert report["head_branch"] == "sieve"
    fp = report["fork_points"][0]
    assert [a["name"] for a in fp["alternatives"]] == ["wrong", "slow", "sieve"]
    by_label = {e["label"]: e for e in fp["evaluations"]}
    assert by_label["wrong"]["passed"] < by_label["wrong"]["total"]
    assert by_label["slow"]["passed"] == by_label["sieve"]["passed"] == 15
    assert by_label["sieve"]["metrics"]["latency_ms"] < by_label["slow"]["metrics"]["latency_ms"]
    assert "better benchmarks" in fp["why"]
    # losers were deleted at promotion time, everything else at the end
    assert len(fp["deleted_nodes"]) == 2
    assert backend.live_ids() == set()
    assert report["leftover_sandboxes"] == []
    assert "count_primes" in report["diff"] and "bytearray" in report["diff"]
    # each branch's model calls saw the shared prefix plus its own turns, with valid tool pairing
    branch_calls = [c for c in llm.calls if c["branch"]]
    assert {c["branch"] for c in branch_calls} == {"wrong", "slow", "sieve"}
    for b, _ in explorer._branches:
        assert_well_formed(b.messages)
    root = explorer._branches[0][0]
    for b, _ in explorer._branches[1:]:
        assert b.messages[: len(root.messages)] == root.messages  # append-only fork
    assert [e["type"] for e in events].count("evaluated") == 3


def test_fork_beyond_depth_limit_is_refused_and_branch_continues():
    def script(branch, turn, messages):
        if branch is None:
            alts = [{"name": "sieve", "plan": "sieve"}, {"name": "slow", "plan": "slow"}]
            return [tool_use("fork", {"alternatives": alts})]
        if turn == 0:  # try to fork again from inside a branch
            alts = [{"name": "a", "plan": "a"}, {"name": "b", "plan": "b"}]
            return [tool_use("fork", {"alternatives": alts})]
        return branch_script(branch, turn - 1)

    explorer, backend, _, events = make(script, max_fork_depth=1)
    report = asyncio.run(explorer.run(demo_task()))
    refused = [e for e in events if e["type"] == "fork_refused"]
    assert len(refused) == 2 and "1 level" in refused[0]["reason"]
    assert report["head_branch"] == "sieve" and len(report["fork_points"]) == 1
    for b, _ in explorer._branches:
        assert_well_formed(b.messages)
    errors = [
        blk
        for m in explorer._branches[1][0].messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for blk in m["content"]
        if blk.get("is_error")
    ]
    assert errors and "Fork refused" in errors[0]["content"]
    assert backend.live_ids() == set()


def test_nested_fork_promotes_grandchild_and_deletes_everything():
    def script(branch, turn, messages):
        if branch is None:
            alts = [{"name": "wrong", "plan": "x"}, {"name": "tune", "plan": "y"}]
            return [tool_use("fork", {"alternatives": alts})]
        if branch == "tune" and turn == 0:
            alts = [{"name": "slow", "plan": "s"}, {"name": "sieve", "plan": "f"}]
            return [tool_use("fork", {"alternatives": alts})]
        return branch_script(branch, turn)

    explorer, backend, _, _ = make(script, max_fork_depth=2)
    report = asyncio.run(explorer.run(demo_task()))
    assert report["head_branch"] == "tune/sieve"
    assert len(report["fork_points"]) == 2
    store = explorer.engine.store
    assert all(n.status is NodeStatus.GC for n in store.run_nodes(report["run_id"]))
    assert backend.live_ids() == set()


def test_orchestrator_initiated_fork_and_crashing_branch():
    def script(branch, turn, messages):
        if branch == "boom":
            raise RuntimeError("model exploded")
        return branch_script(branch, turn)

    alts = [{"name": "boom", "plan": "crash"}, {"name": "sieve", "plan": "sieve"}]
    explorer, backend, llm, _ = make(script, initial_alternatives=alts)
    report = asyncio.run(explorer.run(demo_task()))
    assert report["head_branch"] == "sieve"
    assert all(c["branch"] is not None for c in llm.calls)  # the root agent never ran
    evs = {e["label"]: e for e in report["fork_points"][0]["evaluations"]}
    assert "model exploded" in evs["boom"]["error"]
    assert backend.live_ids() == set()


def test_failed_run_still_deletes_sandboxes():
    explorer, backend, _, _ = make(standard_script)
    task = dataclasses.replace(demo_task(), eval=dataclasses.replace(demo_task().eval, setup=("exit 3",)))
    with pytest.raises(Exception, match="setup command failed"):
        asyncio.run(explorer.run(task))
    assert backend.live_ids() == set()


def test_anthropic_llm_request_shape():
    seen = {}

    class FakeMessages:
        async def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(
                content=[text("hi")], stop_reason="end_turn", usage=None, model=kw["model"]
            )

    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    llm = AnthropicLLM(client=client, effort="high")
    from sfork.agent.tools import ALL_TOOLS

    resp = asyncio.run(
        llm.complete(
            model="claude-opus-5-5",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            tools=ALL_TOOLS,
            max_tokens=1000,
        )
    )
    assert resp.stop_reason == "end_turn"
    assert seen["model"] == "claude-opus-5-5"
    assert seen["output_config"] == {"effort": "high"}
    assert seen["fallbacks"] == "default" and seen["betas"] == ["server-side-fallback-2026-07-01"]
    assert seen["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in seen and "tool_choice" not in seen  # adaptive default; no forced tools
    for t in seen["tools"]:
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


def test_task_load():
    task = Task.load(EXAMPLE)
    assert set(task.files) == {"primes.py", "test_primes.py", "bench.py"}
    assert task.eval.tests is not None and task.eval.benchmarks[0].direction == "lower"
    assert [a["name"] for a in Task.alternatives_from(EXAMPLE)] == ["trial-division", "sieve"]
