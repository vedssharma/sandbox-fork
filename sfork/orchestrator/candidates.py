"""Evaluate N prepared candidate solutions in forked sandboxes and pick the winner (`sfork evaluate`).

This is the evaluator on its own, without an agent: the task is seeded into a root sandbox once
(repo, baseline commit, setup such as a running database), the root is forked once per candidate,
each candidate's script runs in its own fork, and then exactly the same scoring, judge and artifact
storage as `sfork explore` decide and explain the winner. A candidate is a shell script run with
bash from the task's workdir, so it can edit files and also change live state (apply a migration).

    examples/billing_migration/candidates/
        as-committed.sh   round-cast.sh   round-cast-guarded.sh   ...
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.llm import LLM
from ..eval.artifacts import ArtifactStore
from ..eval.evaluator import Evaluation, evaluate, in_dir
from ..eval.select import select_winner
from .engine import TreeEngine
from .explore import Task, seed_task

EventFn = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Candidate:
    name: str
    script: str

    @classmethod
    def load_dir(cls, d: str | Path) -> list[Candidate]:
        files = sorted(Path(d).glob("*.sh"), key=lambda f: f.stem)
        if not files:
            raise FileNotFoundError(f"no *.sh candidates in {d}")
        return [cls(name=f.stem, script=f.read_text()) for f in files]


async def evaluate_candidates(
    engine: TreeEngine,
    task: Task,
    candidates: list[Candidate],
    *,
    llm: LLM | None = None,
    model: str | None = None,
    artifacts_dir: str | Path | None = None,
    apply_timeout: int = 900,
    on_event: EventFn | None = None,
) -> dict[str, Any]:
    emit = on_event or (lambda e: None)
    cfg = task.eval
    t0 = time.monotonic()
    run, root = await engine.start_run(f"evaluate {len(candidates)} candidates for {task.name}")
    report: dict[str, Any] = {"task": task.name, "run_id": run.id, "candidates": [c.name for c in candidates]}
    emit({"type": "run", "run": run.id, "node": root.id, "sandbox": root.sandbox_id})
    try:
        await seed_task(lambda c, t: engine.exec(root.id, c, timeout=t), task)
        emit({"type": "seeded", "node": root.id})
        f0 = time.monotonic()
        children = await engine.fork(root.id, [c.name for c in candidates])
        report["fork_seconds"] = round(time.monotonic() - f0, 3)
        emit({"type": "forked", "children": [c.id for c in children], "seconds": report["fork_seconds"]})

        async def baseline() -> tuple[Evaluation | None, list[str]]:
            if not cfg.baseline:
                return None, []
            node = await engine.scratch_fork(root.id, "baseline (fork point)")
            ev = await evaluate(
                lambda c, t: engine.backend.exec(node.sandbox_id, c, timeout=t),
                cfg,
                node_id=node.id,
                label="baseline",
            )
            emit({"type": "baseline_evaluated", "summary": ev.summary(), "seconds": round(ev.seconds, 2)})
            return (ev if ev.error is None else None), await engine.discard(node.id)

        async def one(node_id: str, cand: Candidate) -> tuple[Evaluation, dict[str, Any]]:
            exec_ = lambda c, t: engine.exec(node_id, c, timeout=t)  # noqa: E731
            a0 = time.monotonic()
            b64 = base64.b64encode(cand.script.encode()).decode()
            cmd = f'f=$(mktemp /tmp/sfork-candidate-XXXXXX.sh) && echo {b64} | base64 -d > "$f" && bash "$f"'
            res = await exec_(in_dir(cfg.workdir, cmd), apply_timeout)
            applied = {
                "exit_code": res.exit_code,
                "seconds": round(time.monotonic() - a0, 2),
                "output": res.output[-4000:],
            }
            emit({"type": "applied", "candidate": cand.name, "exit_code": res.exit_code})
            ev = await evaluate(exec_, cfg, node_id=node_id, label=cand.name)
            ev.artifacts["apply.log"] = f"$ exit {res.exit_code}\n{res.output}"
            emit({"type": "evaluated", "candidate": cand.name, "summary": ev.summary()})
            return ev, applied

        base_task = asyncio.create_task(baseline())
        try:
            outs = await asyncio.gather(*(one(n.id, c) for n, c in zip(children, candidates, strict=True)))
        finally:
            try:
                base, base_deleted = await base_task
            except Exception as e:
                emit({"type": "baseline_skipped", "reason": f"{type(e).__name__}: {e}"})
                base, base_deleted = None, []
        evals = [e for e, _ in outs]
        sel = await select_winner(evals, cfg, baseline=base, llm=llm, model=model, task=task.prompt)
        winner_node = sel.winner.node_id
        deleted = base_deleted + await engine.promote(winner_node)
        if artifacts_dir:
            report["artifacts"] = str(ArtifactStore(artifacts_dir).save_selection(run.id, "candidates", sel))
        report.update(
            status="ok",
            winner=sel.winner.label,
            deciding=sel.deciding,
            why=sel.why,
            selection=sel.as_dict(),
            baseline=base.as_dict() if base else None,
            evaluations=[e.as_dict() for e in evals],
            applied={c.name: a for c, (_, a) in zip(candidates, outs, strict=True)},
            judge=sel.verdict.as_dict() if sel.verdict else None,
            winner_diff=sel.winner.diff[:40_000],
            deleted_at_selection=deleted,
        )
        emit({"type": "promoted", "candidate": sel.winner.label, "why": sel.why})
    except Exception as e:
        report.update(status="error", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        await engine.finish_run(run.id)
        report["tree"] = engine.render_tree(run.id)
        left = []
        for n in engine.store.run_nodes(run.id):
            try:
                st = await engine.backend.state(n.sandbox_id)
            except Exception as e:
                st = f"unknown ({type(e).__name__})"
            if st not in ("destroyed", "killed", "deleted"):
                left.append(f"{n.sandbox_id}:{st}")
        report["leftover_sandboxes"] = left
        report["wall_seconds"] = round(time.monotonic() - t0, 2)
    return report


__all__ = ["Candidate", "evaluate_candidates"]
