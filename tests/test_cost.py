"""$ metering, the hard $ cap, linear mode and post-checks (used by the Phase 4 benchmark)."""

import asyncio

import pytest

from sfork.agent.llm import text, tool_use
from sfork.core.cost import CostMeter, cost_usd
from tests.test_explore import IMPLS, demo_task, make


def test_cost_usd_uses_list_prices_and_cache_rates():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4 + 20 + 0.2 + 5)
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(2 + 10 + 0.2 + 2.5)
    assert cost_usd("unknown-model", {"output_tokens": 1_000_000}) == pytest.approx(20)  # priced as Opus


def test_meter_tracks_cap():
    m = CostMeter(cap_usd=0.001)
    m.record("claude-opus-5-5", {"output_tokens": 40})  # $0.0008
    assert not m.exhausted
    m.record("claude-opus-5-5", {"output_tokens": 40})
    assert m.exhausted and m.as_dict()["calls"] == 2


def busy_script(branch, turn, messages):
    return [tool_use("bash", {"command": "true"})]


def test_cap_stops_agent_and_run_still_reports():
    # ScriptedLLM bills 100 input + 20 output tokens per call: $0.0008 on Opus 5.5
    explorer, backend, llm, events = make(busy_script, cost_cap_usd=0.002)
    report = asyncio.run(explorer.run(demo_task()))
    assert report["status"] == "ok"
    assert report["final_stop"] == "budget" and "$ budget reached" in report["final_summary"]
    assert report["cost"]["calls"] == 3 and report["cost"]["refused_calls"] == 1
    assert report["cost"]["spent_usd"] == pytest.approx(0.0024)
    assert backend.live_ids() == set()


def linear_script(branch, turn, messages):
    if turn == 0:
        return [tool_use("write_file", {"path": "primes.py", "content": IMPLS["sieve"]})]
    return [text("Done."), tool_use("done", {"summary": "sieve"})]


def test_linear_mode_hides_fork_tool_and_runs_post_checks():
    explorer, backend, llm, events = make(
        linear_script,
        forking=False,
        checkpoints=False,
        post_checks={"has_sieve": "grep -q bytearray primes.py", "missing": "test -f nope"},
    )
    report = asyncio.run(explorer.run(demo_task()))
    assert report["status"] == "ok" and report["final_evaluation"]["passed"] == 15
    names = {t["name"] for t in explorer.agent.tools}
    assert "fork" not in names and "checkpoint" not in names and "bash" in names
    assert "You can fork" not in explorer.agent.system and "Work is ranked" in explorer.agent.system
    assert report["post_checks"]["has_sieve"]["exit_code"] == 0
    assert report["post_checks"]["missing"]["exit_code"] != 0
    assert report["cost"]["calls"] == 2 and report["cost"]["cap_usd"] is None
    assert backend.live_ids() == set()


def test_best_of_n_branches_share_the_cap_without_fork_tool():
    def script(branch, turn, messages):
        return linear_script(branch, turn, messages)

    alts = [{"name": f"sample-{i}", "plan": "solve it"} for i in (1, 2, 3)]
    explorer, backend, llm, events = make(
        script, forking=False, checkpoints=False, initial_alternatives=alts, cost_cap_usd=1.0
    )
    report = asyncio.run(explorer.run(demo_task()))
    assert report["status"] == "ok" and len(report["fork_points"]) == 1
    assert report["cost"]["calls"] == 6  # 2 per sample, metered on one shared meter
    assert "fork" not in {t["name"] for t in explorer.agent.tools}
    assert "Branches are ranked" in explorer.agent.system
