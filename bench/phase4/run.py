"""Phase 4 benchmark: linear agent vs best-of-N vs agent-initiated forking at an equal $ budget.

    python bench/phase4/run.py --tasks all --conditions linear,best_of_n,fork --repeats 2 \
        --out bench/results/phase4

Conditions (same model, effort, step limit and hard $ cap per run on a given task):

- linear     one agent, no fork tool, no checkpoint/rewind. Plain tools: bash, files, run_tests.
- best_of_n  the orchestrator forks the seeded sandbox N ways before the agent starts; N linear
             agents run in parallel and the evaluator keeps the best one. The cap is shared.
- fork       one agent with the fork tool and checkpoint/rewind (auto-checkpoints before risky
             commands). The cap is shared by every branch.

Each run records: solved (see below), wall clock, $ spent (from token usage at list prices; see
sfork.core.cost), and, for stateful tasks, whether the run ended with irreversibly damaged state
("dirty"): a task's dirty_check script runs on the final head before teardown and reports data
that no longer exists anywhere on the machine.

Solved = the final head passes every gate and every visible test (and is not disqualified), plus:
SWE-bench tasks must pass their hidden FAIL_TO_PASS + PASS_TO_PASS tests (the visible tests don't
count there), and tasks with max_metric must beat the threshold.

Results: one JSON report per run (<out>/runs/<task>.<condition>.<rep>.json, full transcripts
included) and a summary (<out>/summary.json, <out>/summary.md). Re-running skips runs that already
have a report, so an interrupted benchmark resumes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.phase4.swe import hidden_check, load_instances  # noqa: E402
from sfork.agent.llm import AnthropicLLM  # noqa: E402
from sfork.core.budget import Budget  # noqa: E402
from sfork.core.store import TreeStore  # noqa: E402
from sfork.daytona import make_backend  # noqa: E402
from sfork.orchestrator.engine import TreeEngine  # noqa: E402
from sfork.orchestrator.explore import ExploreConfig, Explorer, Task  # noqa: E402

CONDITIONS = ("linear", "best_of_n", "fork", "forced_fork", "linear_careful")
# Phase 7 control: a linear agent told what the rewind prompt tells it about risk, without the tools.
CAREFUL_NOTE = (
    "Some commands can damage state you can't cleanly repair (data lost or corrupted, a half-applied "
    "migration, a broken environment), and nothing on this machine can undo them for you."
)
SAMPLE_PLAN = "Solve the whole task independently, your own way, as if you were the only attempt."


def load_suite(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def load_task(spec: dict[str, Any]) -> Task:
    task = Task.load(ROOT / spec["path"])
    if spec.get("prompt"):
        task.prompt = spec["prompt"]
    return task


def post_checks(spec: dict[str, Any]) -> dict[str, str]:
    checks: dict[str, str] = {}
    if spec.get("dirty_check"):
        src = (ROOT / spec["dirty_check"]).read_text()
        checks["dirty"] = f"python3 - <<'SFORK_CHECK'\n{src}\nSFORK_CHECK"
    if spec.get("swe_instance"):
        checks["hidden_tests"] = hidden_check(load_instances()[spec["swe_instance"]])
    return checks


def explore_config(suite: dict[str, Any], spec: dict[str, Any], condition: str) -> ExploreConfig:
    steps = int(suite.get("max_steps", 40))
    base = dict(
        model=suite.get("model", "claude-opus-5-5"),
        max_steps_root=steps,
        max_steps_branch=steps,
        cost_cap_usd=round(float(spec["budget_usd"]) * float(suite.get("budget_scale", 1.0)), 4),
        post_checks=post_checks(spec),
        transcripts=True,
        branch_timeout_s=1800,
    )
    if condition == "linear":
        return ExploreConfig(**base, forking=False, checkpoints=False, auto_checkpoint=False)
    if condition == "linear_careful":
        return ExploreConfig(
            **base, forking=False, checkpoints=False, auto_checkpoint=False, system_note=CAREFUL_NOTE
        )
    if condition == "best_of_n":
        n = int(suite.get("best_of_n", 3))
        alts = [{"name": f"sample-{i + 1}", "plan": SAMPLE_PLAN} for i in range(n)]
        return ExploreConfig(
            **base,
            forking=False,
            checkpoints=False,
            auto_checkpoint=False,
            initial_alternatives=alts,
            max_fanout=n,
        )
    if condition in ("fork", "forced_fork"):
        return ExploreConfig(
            **base,
            forking=True,
            checkpoints=True,
            auto_checkpoint=True,
            max_fanout=3,
            max_fork_depth=1,
            forced_fork=condition == "forced_fork",
        )
    raise ValueError(condition)


def judge_solved(spec: dict[str, Any], rep: dict[str, Any]) -> dict[str, Any]:
    ev = rep.get("final_evaluation") or {}
    checks = rep.get("post_checks") or {}
    out: dict[str, Any] = {"status": rep.get("status")}
    gates_ok = bool(ev) and ev.get("error") is None and all((ev.get("gates") or {}).values())
    out["gates_ok"] = gates_ok
    out["disqualified"] = ev.get("disqualified")
    out["tests"] = f"{ev.get('passed')}/{ev.get('total')}"
    if spec.get("swe_instance"):
        hidden = checks.get("hidden_tests", {})
        out["hidden_ok"] = hidden.get("exit_code") == 0
        solved = gates_ok and not ev.get("disqualified") and out["hidden_ok"]
    else:
        tests_ok = bool(ev.get("total")) and ev.get("passed") == ev.get("total")
        solved = gates_ok and not ev.get("disqualified") and tests_ok
        for name, limit in (spec.get("max_metric") or {}).items():
            v = (ev.get("metrics") or {}).get(name)
            out[name] = v
            solved = solved and v is not None and v <= limit
    if spec.get("dirty_check"):
        d = checks.get("dirty", {})
        out["dirty"] = d.get("exit_code") not in (0, None)
        try:
            out["lost_records"] = json.loads(d.get("output", "").strip().splitlines()[-1]).get("lost")
        except (ValueError, IndexError, AttributeError):
            out["lost_records"] = None
    out["solved"] = bool(solved)
    return out


def summarize_run(spec: dict[str, Any], condition: str, rep_i: int, rep: dict[str, Any]) -> dict[str, Any]:
    v = judge_solved(spec, rep)
    branches = rep.get("branches") or []
    events = rep.get("events") or []
    return {
        "task": spec["name"],
        "kind": spec.get("kind"),
        "condition": condition,
        "rep": rep_i,
        **v,
        "cost_usd": (rep.get("cost") or {}).get("spent_usd"),
        "cap_usd": (rep.get("cost") or {}).get("cap_usd"),
        "hit_cap": bool((rep.get("cost") or {}).get("refused_calls")),
        "wall_s": rep.get("wall_seconds"),
        "llm_calls": (rep.get("cost") or {}).get("calls"),
        "forks": len(rep.get("fork_points") or []),
        "checkpoints": len(rep.get("checkpoints") or []),
        "rewinds": len(rep.get("rewinds") or []),
        "decision_points": sum(e.get("type") == "decision_point" for e in events),
        "fallback_forks": sum(e.get("type") == "fork_request" and bool(e.get("forced")) for e in events),
        "branches": len(branches),
        "head_branch": rep.get("head_branch"),
        "stops": [b.get("stop") for b in branches],
        "leftover_sandboxes": rep.get("leftover_sandboxes"),
        "error": rep.get("error"),
    }


async def run_one(
    suite: dict[str, Any], spec: dict[str, Any], condition: str, rep_i: int, out: Path, db: Path
) -> dict[str, Any]:
    path = out / "runs" / f"{spec['name']}.{condition}.{rep_i}.json"
    if path.exists():
        rep = json.loads(path.read_text())
        return summarize_run(spec, condition, rep_i, rep)
    task = load_task(spec)
    cfg = explore_config(suite, spec, condition)
    events: list[dict[str, Any]] = []
    be = make_backend("e2b")
    model = suite.get("model", "claude-opus-5-5")
    # Server-side refusal fallbacks aren't offered for Haiku.
    llm = AnthropicLLM(effort=suite.get("effort", "medium"), fallbacks=not model.startswith("claude-haiku"))
    engine = TreeEngine(TreeStore(db), be, budget=Budget(max_fanout=4, max_running_sandboxes=8))
    t0 = time.monotonic()
    tag = f"{spec['name']}.{condition}.{rep_i}"
    print(f"[{tag}] start", flush=True)
    try:
        rep = await Explorer(engine, llm, cfg, on_event=events.append).run(task)
    except Exception as e:  # the explorer re-raises after cleanup; keep what we know
        rep = {
            "task": task.name,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-4000:],
            "wall_seconds": round(time.monotonic() - t0, 2),
        }
    finally:
        await llm.close()
        await be.close()
    rep["events"] = events
    rep["condition"] = condition
    rep["bench_spec"] = {k: v for k, v in spec.items() if k != "prompt"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep, indent=1, default=str))
    s = summarize_run(spec, condition, rep_i, rep)
    print(
        f"[{tag}] solved={s['solved']} dirty={s.get('dirty')} ${s['cost_usd']} {s['wall_s']}s "
        f"forks={s['forks']} rewinds={s['rewinds']} err={s['error']}",
        flush=True,
    )
    return s


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        solves = sum(r["solved"] for r in rs)
        cost = sum(r["cost_usd"] or 0 for r in rs)
        walls = [r["wall_s"] for r in rs if r["wall_s"] is not None]
        dirty = [r for r in rs if r.get("dirty") is not None]
        return {
            "runs": n,
            "solved": solves,
            "solve_rate": round(solves / n, 3) if n else None,
            "cost_usd": round(cost, 4),
            "mean_cost_usd": round(cost / n, 4) if n else None,
            "usd_per_solve": round(cost / solves, 4) if solves else None,
            "median_wall_s": round(statistics.median(walls), 1) if walls else None,
            "dirty_runs": sum(bool(r["dirty"]) for r in dirty) if dirty else None,
            "stateful_runs": len(dirty) or None,
            "rewinds": sum(r["rewinds"] for r in rs),
            "agent_fork_points": sum(r["forks"] for r in rs if r["condition"] in ("fork", "forced_fork")),
            "fallback_forks": sum(r.get("fallback_forks", 0) for r in rs),
            "hit_cap": sum(r["hit_cap"] for r in rs),
            "errors": sum(r["status"] == "error" for r in rs),
        }

    by_cond = {c: agg([r for r in rows if r["condition"] == c]) for c in CONDITIONS}
    by_task: dict[str, dict[str, Any]] = {}
    for t in dict.fromkeys(r["task"] for r in rows):
        by_task[t] = {c: agg([r for r in rows if r["task"] == t and r["condition"] == c]) for c in CONDITIONS}
    by_kind: dict[str, dict[str, Any]] = {}
    for k in dict.fromkeys(r["kind"] for r in rows):
        by_kind[k] = {c: agg([r for r in rows if r["kind"] == k and r["condition"] == c]) for c in CONDITIONS}
    return {"by_condition": by_cond, "by_task": by_task, "by_kind": by_kind}


def render_md(summary: dict[str, Any], rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    def table(groups: dict[str, dict[str, Any]], key_name: str) -> list[str]:
        lines = [
            f"| {key_name} | condition | solved | $ total | $ / solve | median wall (s) | dirty end state | rewinds | forks | hit cap |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for k, conds in groups.items():
            for c, a in conds.items():
                if not a["runs"]:
                    continue
                dirty = "n/a" if a["stateful_runs"] is None else f"{a['dirty_runs']}/{a['stateful_runs']}"
                lines.append(
                    f"| {k} | {c} | {a['solved']}/{a['runs']} | {a['cost_usd']:.2f} | "
                    f"{'-' if a['usd_per_solve'] is None else f'{a["usd_per_solve"]:.3f}'} | "
                    f"{a['median_wall_s']} | {dirty} | {a['rewinds']} | {a['agent_fork_points']} | {a['hit_cap']} |"
                )
        return lines

    out = [
        f"# {meta.get('title', 'Benchmark')} summary",
        "",
        f"Model {meta['model']} (effort {meta['effort']}), best-of-{meta['best_of_n']}, "
        f"{meta['repeats']} repeat(s), E2B. Total spend ${meta['total_usd']:.2f}.",
        "",
        "## By condition",
        "",
        *table({"all": summary["by_condition"]}, "tasks"),
        "",
        "## By task kind",
        "",
        *table(summary["by_kind"], "kind"),
        "",
        "## By task",
        "",
        *table(summary["by_task"], "task"),
        "",
        "## Runs",
        "",
        "| task | condition | rep | solved | tests | dirty | $ | wall (s) | forks | ckpts | rewinds | head |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['task']} | {r['condition']} | {r['rep']} | {'yes' if r['solved'] else 'no'} | {r['tests']} | "
            f"{'-' if r.get('dirty') is None else r['dirty']} | {r['cost_usd']} | {r['wall_s']} | {r['forks']} | "
            f"{r['checkpoints']} | {r['rewinds']} | {r['head_branch'] or 'root'} |"
        )
    return "\n".join(out) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", default=str(Path(__file__).parent / "suite.yaml"))
    ap.add_argument("--tasks", default="all", help="comma-separated task names, or all")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--first-rep", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=2, help="runs at once (each uses up to ~6 sandboxes)")
    ap.add_argument("--out", default="bench/results/phase4")
    ap.add_argument("--model", help="override the suite's model")
    ap.add_argument("--budget-scale", type=float, default=1.0, help="multiply every task's $ cap")
    ap.add_argument("--max-total-usd", type=float, default=40.0, help="stop starting runs past this spend")
    args = ap.parse_args()

    suite = load_suite(Path(args.suite))
    if args.model:
        suite["model"] = args.model
    suite["budget_scale"] = args.budget_scale
    specs = (
        suite["tasks"]
        if args.tasks == "all"
        else [t for t in suite["tasks"] if t["name"] in args.tasks.split(",")]
    )
    conds = [c for c in args.conditions.split(",") if c]
    out = Path(args.out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    db = out / "tree.db"

    jobs = [
        (spec, cond, rep)
        for rep in range(args.first_rep, args.first_rep + args.repeats)
        for spec in specs
        for cond in conds
    ]
    sem = asyncio.Semaphore(args.concurrency)
    spent = 0.0
    rows: list[dict[str, Any]] = []

    async def guarded(spec: dict[str, Any], cond: str, rep: int) -> None:
        nonlocal spent
        async with sem:
            if spent >= args.max_total_usd:
                print(f"skip {spec['name']}.{cond}.{rep}: total spend ${spent:.2f} reached the limit")
                return
            r = await run_one(suite, spec, cond, rep, out, db)
            spent += r["cost_usd"] or 0
            rows.append(r)

    await asyncio.gather(*(guarded(*j) for j in jobs))
    # every finished run in the output folder, not only this invocation's
    rows = []
    for p in sorted((out / "runs").glob("*.json")):
        task, cond, rep = p.stem.rsplit(".", 2)
        spec = next((s for s in suite["tasks"] if s["name"] == task), None)
        if spec is None:
            continue
        rows.append(summarize_run(spec, cond, int(rep), json.loads(p.read_text())))
    rows.sort(
        key=lambda r: (
            [s["name"] for s in suite["tasks"]].index(r["task"]),
            CONDITIONS.index(r["condition"]),
            r["rep"],
        )
    )
    summary = aggregate(rows)
    meta = {
        "title": suite.get("title", "Phase 4 benchmark"),
        "model": suite.get("model"),
        "effort": suite.get("effort"),
        "best_of_n": suite.get("best_of_n"),
        "budget_scale": suite.get("budget_scale"),
        "repeats": max((r["rep"] for r in rows), default=0),
        "total_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary, "runs": rows}, indent=1))
    (out / "summary.md").write_text(render_md(summary, rows, meta))
    print((out / "summary.md").read_text())


if __name__ == "__main__":
    if "ANTHROPIC_API_KEY" not in os.environ and os.environ.get("SFORK_ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["SFORK_ANTHROPIC_API_KEY"]
        os.environ.pop("ANTHROPIC_BASE_URL", None)  # the cloud session's own proxy, not for this key
    asyncio.run(main())
