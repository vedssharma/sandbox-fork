"""The fork-evaluate-promote loop with an agent in it (PLAN.md sections 4.1 and 5).

    root agent works ──▶ calls fork([A, B, C]) ──▶ engine forks the sandbox 3 ways (parent pauses)
                                                    conversation copied into each child
        branch A ─┐
        branch B ─┼─ run concurrently until done / step budget / timeout
        branch C ─┘
                  ──▶ evaluator runs gates, tests, benchmarks in each branch
                  ──▶ winner promoted (becomes head), losers deleted
                  ──▶ diff + report extracted from the head; every sandbox deleted at the end

Forks can nest up to `max_fork_depth`. The orchestrator can also fork the root itself before the
agent starts (`initial_alternatives`: best-of-N with real environments).
"""

from __future__ import annotations

import asyncio
import re
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.llm import DEFAULT_MODEL, LLM, Usage
from ..agent.runtime import Agent, Branch, Stop, write_file
from ..core.budget import BudgetExceeded
from ..core.models import NodeStatus
from ..eval.config import EvalConfig
from ..eval.evaluator import Evaluation, evaluate, explain, in_dir, rank
from .engine import EngineError, TreeEngine

EventFn = Callable[[dict[str, Any]], None]

SYSTEM_PROMPT = """\
You are a software engineer working on a project inside a Linux sandbox, through tools. The project \
is in {workdir} and commands run from there. Work efficiently: look at what you need, make the \
change, check it with run_tests, then call done.

You can fork. When the task has a real design choice between materially different approaches and \
you can't tell in advance which will score best, call fork with those alternatives instead of \
guessing: each one runs in its own copy of this machine and conversation, and only the best branch \
is kept. If you have been forked into a branch, commit to its approach.

Branches are ranked by: {ranking}."""


@dataclass
class Task:
    name: str
    prompt: str
    eval: EvalConfig
    files: dict[str, str] = field(default_factory=dict)  # path relative to workdir -> content

    @classmethod
    def load(cls, path: str | Path) -> Task:
        """Load a task directory: task.md (prompt), sandbox-fork.toml (eval config), repo/ (seed files)."""
        d = Path(path)
        repo = d / "repo"
        files = {}
        if repo.is_dir():
            for f in sorted(repo.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    files[f.relative_to(repo).as_posix()] = f.read_text()
        return cls(
            name=d.name,
            prompt=(d / "task.md").read_text(),
            eval=EvalConfig.load(d / "sandbox-fork.toml"),
            files=files,
        )

    @staticmethod
    def alternatives_from(path: str | Path) -> list[dict[str, str]]:
        with open(Path(path) / "sandbox-fork.toml", "rb") as f:
            return list(tomllib.load(f).get("alternatives", []))


@dataclass
class ExploreConfig:
    model: str = DEFAULT_MODEL
    worker_model: str | None = None  # model for forked branches; defaults to `model`
    max_steps_root: int = 30
    max_steps_branch: int = 30
    max_fanout: int = 3
    max_fork_depth: int = 1  # 1 = branches may not fork again
    branch_timeout_s: float = 1800
    tool_timeout_s: int = 300
    max_tokens: int = 16_000
    keep_head: bool = False  # leave the winning sandbox running after the run
    initial_alternatives: list[dict[str, str]] | None = None


@dataclass
class Result:
    branch: Branch
    evaluation: Evaluation
    stop: Stop


class Explorer:
    def __init__(
        self,
        engine: TreeEngine,
        llm: LLM,
        cfg: ExploreConfig | None = None,
        *,
        on_event: EventFn | None = None,
    ) -> None:
        self.engine = engine
        self.llm = llm
        self.cfg = cfg or ExploreConfig()
        self.on_event = on_event or (lambda e: None)
        self._branches: list[tuple[Branch, Stop | None]] = []
        self._fork_points: list[dict[str, Any]] = []
        self.agent: Agent | None = None

    async def run(self, task: Task) -> dict[str, Any]:
        cfg, engine = self.cfg, self.engine
        ranking = ["gates pass", "test pass rate"] + [
            f"benchmark {b.name} ({b.direction} is better)" for b in task.eval.benchmarks
        ]
        self.agent = Agent(
            self.llm,
            self._exec,
            task.eval,
            on_event=self._emit,
            max_tokens=cfg.max_tokens,
            tool_timeout=cfg.tool_timeout_s,
            system=SYSTEM_PROMPT.format(workdir=task.eval.workdir, ranking=", then ".join(ranking)),
        )
        t0 = time.monotonic()
        run, root = await engine.start_run(task.prompt.strip().splitlines()[0][:200])
        self._emit(
            {"type": "run", "run": run.id, "node": root.id, "sandbox": root.sandbox_id, "task": task.name}
        )
        report: dict[str, Any] = {
            "task": task.name,
            "run_id": run.id,
            "model": cfg.model,
            "worker_model": cfg.worker_model or cfg.model,
        }
        try:
            await self._seed(root.id, task)
            br = Branch(node_id=root.id, model=cfg.model, messages=[{"role": "user", "content": task.prompt}])
            self._branches.append((br, None))
            if cfg.initial_alternatives:
                stop = Stop("fork", alternatives=list(cfg.initial_alternatives))
                result = await self._explore(br, stop, depth=0)
            else:
                result = await self._drive(br, depth=0, max_steps=cfg.max_steps_root)
            report.update(
                status="ok",
                head=result.branch.node_id,
                head_branch=result.branch.name,
                final_stop=result.stop.kind,
                final_summary=result.stop.summary,
                final_evaluation=result.evaluation.as_dict(),
                diff=await self._diff(result.branch.node_id, task.eval.workdir),
            )
        except Exception as e:
            report.update(status="error", error=f"{type(e).__name__}: {e}")
            raise
        finally:
            report["tree"] = engine.render_tree(run.id)
            report["fork_points"] = self._fork_points
            report["branches"] = [
                {
                    "node": b.node_id,
                    "name": b.name,
                    "model": b.model,
                    "steps": b.steps,
                    "tool_calls": b.tool_calls,
                    "stop": s.kind if s else None,
                    "summary": s.summary[:1000] if s else None,
                    "usage": b.usage.as_dict(),
                }
                for b, s in self._branches
            ]
            report["usage_by_model"] = self._usage_by_model()
            deleted = await engine.finish_run(run.id, keep_head=cfg.keep_head)
            report["sandboxes_deleted"] = len(deleted) + sum(
                len(fp["deleted_nodes"]) for fp in self._fork_points
            )
            report["leftover_sandboxes"] = await self._leftovers(run.id)
            report["wall_seconds"] = round(time.monotonic() - t0, 2)
            self._emit(
                {
                    "type": "finished",
                    "run": run.id,
                    "deleted": len(deleted),
                    "leftover": report["leftover_sandboxes"],
                }
            )
        return report

    # the loop ---------------------------------------------------------------------------------------
    async def _drive(self, br: Branch, *, depth: int, max_steps: int, timeout: float | None = None) -> Result:
        assert self.agent is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                left = None if deadline is None else max(1.0, deadline - time.monotonic())
                stop = await asyncio.wait_for(self.agent.run(br, max_steps=max_steps), left)
            except TimeoutError:
                stop = Stop("budget", summary=f"branch timed out after {timeout}s")
            if stop.kind == "fork":
                reason = self._fork_refusal(stop, depth)
                if reason is None:
                    try:
                        return await self._explore(br, stop, depth=depth)
                    except (EngineError, BudgetExceeded) as e:
                        reason = str(e)
                self._emit({"type": "fork_refused", "node": br.node_id, "branch": br.name, "reason": reason})
                self.agent.resume_after_failed_fork(br, stop, reason)
                continue
            self._set_stop(br, stop)
            ev = await self._evaluate(br)
            return Result(br, ev, stop)

    async def _explore(self, br: Branch, stop: Stop, *, depth: int) -> Result:
        alts = _normalize(stop.alternatives)
        hyps = [f"{a['name']}: {a['plan']}" for a in alts]
        t0 = time.monotonic()
        children = await self.engine.fork(br.node_id, hyps)
        fork_s = round(time.monotonic() - t0, 3)
        names = [a["name"] for a in alts]
        self._emit(
            {
                "type": "forked",
                "node": br.node_id,
                "branch": br.name,
                "children": [c.id for c in children],
                "alternatives": names,
                "fork_seconds": fork_s,
            }
        )
        worker = self.cfg.worker_model or self.cfg.model
        kids = []
        for child, alt in zip(children, alts, strict=True):
            label = f"{br.name}/{alt['name']}" if br.name else alt["name"]
            kb = br.fork(
                node_id=child.id,
                model=worker,
                name=label,
                plan=alt["plan"],
                siblings=names,
                tool_use_id=stop.tool_use_id,
                pending_results=stop.pending_results,
            )
            kids.append(kb)
            self._branches.append((kb, None))

        async def guarded(kb: Branch) -> Result:
            try:
                return await self._drive(
                    kb,
                    depth=depth + 1,
                    max_steps=self.cfg.max_steps_branch,
                    timeout=self.cfg.branch_timeout_s,
                )
            except Exception as e:  # a crashed branch loses; it doesn't take the run down
                st = Stop("failed", summary=f"{type(e).__name__}: {e}")
                self._set_stop(kb, st)
                return Result(kb, Evaluation(node_id=kb.node_id, label=kb.name, error=st.summary), st)

        results = await asyncio.gather(*(guarded(k) for k in kids))
        ranked = rank([r.evaluation for r in results], self._eval_cfg.benchmarks)
        wi = next(i for i, r in enumerate(results) if r.evaluation is ranked[0])
        winner = results[wi]
        deleted = await self.engine.promote(children[wi].id)
        if winner.branch.node_id != children[wi].id:  # the winner forked again; its own winner is deeper
            deleted += await self.engine.promote(winner.branch.node_id)
        why = explain(ranked)
        self._fork_points.append(
            {
                "node": br.node_id,
                "branch": br.name,
                "fork_seconds": fork_s,
                "alternatives": alts,
                "children": [c.id for c in children],
                "evaluations": [r.evaluation.as_dict() for r in results],
                "winner": winner.branch.name,
                "winner_node": winner.branch.node_id,
                "why": why,
                "deleted_nodes": deleted,
            }
        )
        self._emit(
            {
                "type": "promoted",
                "node": winner.branch.node_id,
                "branch": winner.branch.name,
                "why": why,
                "deleted": len(deleted),
            }
        )
        return winner

    # helpers ----------------------------------------------------------------------------------------
    @property
    def _eval_cfg(self) -> EvalConfig:
        assert self.agent is not None
        return self.agent.eval_cfg

    def _fork_refusal(self, stop: Stop, depth: int) -> str | None:
        if depth >= self.cfg.max_fork_depth:
            return f"this run allows forking only {self.cfg.max_fork_depth} level(s) deep."
        n = len(_normalize(stop.alternatives))
        if n < 2:
            return "give at least 2 distinct alternatives."
        if n > self.cfg.max_fanout:
            return f"at most {self.cfg.max_fanout} alternatives are allowed."
        return None

    async def _exec(self, node_id: str, command: str, timeout: int):
        return await self.engine.exec(node_id, command, timeout=timeout)

    async def _evaluate(self, br: Branch) -> Evaluation:
        node = self.engine.store.get_node(br.node_id)
        node.status = NodeStatus.EVALUATING
        self.engine.store.put_node(node)
        try:
            ev = await evaluate(
                lambda c, t: self.engine.backend.exec(node.sandbox_id, c, timeout=t),
                self._eval_cfg,
                node_id=br.node_id,
                label=br.name,
            )
        finally:
            node.status = NodeStatus.RUNNING
            self.engine.store.put_node(node)
        node.meta["evaluation"] = {
            "passed": ev.passed,
            "total": ev.total,
            "metrics": ev.metrics,
            "gates": ev.gates,
            "error": ev.error,
        }
        self.engine.store.put_node(node)
        self._emit(
            {
                "type": "evaluated",
                "node": br.node_id,
                "branch": br.name,
                "summary": ev.summary(),
                "seconds": round(ev.seconds, 2),
            }
        )
        return ev

    async def _seed(self, node_id: str, task: Task) -> None:
        wd = task.eval.workdir
        res = await self._exec(node_id, f"mkdir -p {wd}", 60)
        if not res.ok:
            raise EngineError(f"could not create {wd}: {res.output[-500:]}")
        for rel, content in task.files.items():
            res = await write_file(lambda c, t: self._exec(node_id, c, t), f"{wd}/{rel}", content)
            if not res.ok:
                raise EngineError(f"could not write {rel}: {res.output[-500:]}")
        # A baseline commit makes the deliverable a plain `git diff`. Skipped if git is missing.
        await self._exec(
            node_id,
            in_dir(
                wd,
                f"command -v git >/dev/null && git init -q && git add -A && "
                f"{_GIT} commit -qm baseline || true",
            ),
            120,
        )
        for cmd in task.eval.setup:
            res = await self._exec(node_id, in_dir(wd, cmd), 900)
            if not res.ok:
                raise EngineError(f"setup command failed ({cmd}): {res.output[-1000:]}")
        self._emit({"type": "seeded", "node": node_id, "files": len(task.files)})

    async def _diff(self, node_id: str, workdir: str) -> str:
        res = await self._exec(
            node_id,
            in_dir(
                workdir,
                f"command -v git >/dev/null && git add -A && "
                f"{_GIT} diff --cached --stat && {_GIT} diff --cached",
            ),
            120,
        )
        out = res.output if res.ok else ""
        return out if len(out) <= 40_000 else out[:40_000] + "\n...[diff truncated]"

    async def _leftovers(self, run_id: str) -> list[str]:
        left = []
        for n in self.engine.store.run_nodes(run_id):
            if self.cfg.keep_head and n.status is not NodeStatus.GC:
                continue
            try:
                state = await self.engine.backend.state(n.sandbox_id)
            except Exception as e:
                state = f"unknown ({type(e).__name__})"
            if state not in ("destroyed", "killed", "deleted"):
                left.append(f"{n.sandbox_id}:{state}")
        return left

    def _set_stop(self, br: Branch, stop: Stop) -> None:
        for i, (b, _) in enumerate(self._branches):
            if b is br:
                self._branches[i] = (b, stop)

    def _usage_by_model(self) -> dict[str, dict[str, int]]:
        out: dict[str, Usage] = {}
        for b, _ in self._branches:
            out.setdefault(b.model, Usage())
            u = out[b.model]
            for k, v in b.usage.as_dict().items():
                setattr(u, k, getattr(u, k) + v)
        return {m: u.as_dict() for m, u in out.items()}

    def _emit(self, event: dict[str, Any]) -> None:
        self.on_event({"t": round(time.time(), 3), **event})


_GIT = "git -c user.email=sfork@localhost -c user.name=sfork -c core.pager=cat"


def _normalize(alts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Clean names (short, unique, path-safe) and drop empty alternatives."""
    out, seen = [], set()
    for a in alts or []:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(a.get("name", "")).strip()).strip("-")[:40] or "alt"
        plan = str(a.get("plan", "")).strip() or name
        base, i = name, 2
        while name in seen:
            name, i = f"{base}-{i}", i + 1
        seen.add(name)
        out.append({"name": name, "plan": plan})
    return out
