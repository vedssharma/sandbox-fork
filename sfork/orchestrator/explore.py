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

Checkpoints and rewind (PLAN.md section 4.5) work along one branch's timeline:

    working node ──checkpoint──▶ checkpoint node (paused) ──▶ new working node ── risky step breaks things
                                        │
                                        └── rewind ──▶ fresh working node (a new fork of the checkpoint)
                                                       conversation cut back to the checkpoint + lesson
                                                       the broken node and everything after it deleted

The agent can call `checkpoint` itself, and risky bash commands (see `sfork.agent.policy`) are
checkpointed automatically before they run. Rewinding never mutates the checkpoint, so the agent can
rewind to the same point again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.llm import DEFAULT_MODEL, LLM, Usage
from ..agent.policy import CheckpointPolicy
from ..agent.runtime import Agent, Branch, Stop, write_file
from ..core.budget import BudgetExceeded
from ..core.cost import BudgetedLLM, CostMeter
from ..core.models import NodeStatus
from ..eval.artifacts import ArtifactStore
from ..eval.config import EvalConfig, load_raw
from ..eval.evaluator import BASE_TAG, Evaluation, evaluate, in_dir
from ..eval.select import select_winner
from .engine import EngineError, TreeEngine

EventFn = Callable[[dict[str, Any]], None]

SYSTEM_PROMPT = """\
You are a software engineer working on a project inside a Linux sandbox, through tools. The project \
is in {workdir} and commands run from there. Work efficiently: look at what you need, make the \
change, check it with run_tests, then call done.
{fork}{rewind}
{ranked} by: {ranking}."""

FORK_PROMPT = """
You can fork. When the task has a real design choice between materially different approaches and \
you can't tell in advance which will score best, call fork with those alternatives instead of \
guessing: each one runs in its own copy of this machine and conversation, and only the best branch \
is kept. If you have been forked into a branch, commit to its approach.
"""

FORCED_FORK_PROMPT = """\
In this run, explore before committing: when you are about to make your first change, you will be \
asked to fork into 2-3 materially different approaches instead.
"""

REWIND_PROMPT = """
You can step back in time. checkpoint saves the whole machine (files, databases, processes) and \
this conversation; rewind returns to a checkpoint exactly, with everything after it undone. Risky \
commands such as migrations and destructive SQL are checkpointed automatically just before they \
run, and their output says which checkpoint to rewind to. So you don't need ad hoc backups. If a \
step damages state you can't cleanly repair (data lost or corrupted, a half-applied migration, a \
broken environment), rewind to the checkpoint before it rather than patching over the damage, and \
write a lesson saying what to do differently.
"""


@dataclass
class Task:
    name: str
    prompt: str
    eval: EvalConfig
    files: dict[str, str] = field(default_factory=dict)  # path relative to workdir -> content
    # Written under FIXTURES_DIR before setup and outside git, for setup-only material (e.g. a data
    # generator the agent shouldn't be able to re-run). Setup commands may delete them.
    fixtures: dict[str, str] = field(default_factory=dict)
    executable: tuple[str, ...] = ()  # files (relative to workdir) seeded with the exec bit
    checkpoint_policy: CheckpointPolicy | None = field(default_factory=CheckpointPolicy)

    @classmethod
    def load(cls, path: str | Path) -> Task:
        """Load a task directory: task.md (prompt), sandbox-fork.yaml (eval config), repo/ (seed files)."""
        d = Path(path)
        files, fixtures = _read_tree(d / "repo"), _read_tree(d / "fixtures")
        executable = tuple(rel for rel in files if os.access(d / "repo" / rel, os.X_OK))
        raw = load_raw(d)
        return cls(
            name=d.name,
            prompt=(d / "task.md").read_text(),
            eval=EvalConfig.from_dict(raw),
            files=files,
            fixtures=fixtures,
            executable=executable,
            checkpoint_policy=CheckpointPolicy.from_config(raw.get("checkpoints")),
        )

    @staticmethod
    def alternatives_from(path: str | Path) -> list[dict[str, str]]:
        return list(load_raw(path).get("alternatives", []))


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
    checkpoints: bool = True  # give the agent checkpoint/rewind tools
    auto_checkpoint: bool = True  # checkpoint before risky commands (needs `checkpoints`)
    max_checkpoints: int = 8  # per run; each one keeps a paused sandbox until the run ends
    max_rewinds: int = 4  # per branch
    transcripts: bool = True  # include each branch's final conversation in the report
    artifacts_dir: str | None = None  # store every evaluation's raw outputs here (see eval.artifacts)
    forking: bool = True  # give the agent the fork tool (False: a linear agent; see bench/phase4.py)
    # hold back the root agent's first state-changing call and have it fork there (needs `forking`)
    forced_fork: bool = False
    cost_cap_usd: float | None = None  # hard $ cap on the run's agent calls (see core.cost)
    # name -> shell command, run in the head's workdir after the final evaluation, before teardown
    post_checks: dict[str, str] = field(default_factory=dict)


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
        self._checkpoints: list[dict[str, Any]] = []
        self._rewinds: list[dict[str, Any]] = []
        self.agent: Agent | None = None
        self._task: Task | None = None
        self._run_id = ""
        self.meter = CostMeter(cap_usd=self.cfg.cost_cap_usd)
        # The agent stops at the cap; the judge (a selection cost) is metered but never refused.
        self._agent_llm = BudgetedLLM(llm, self.meter, enforce=True)
        self._judge_llm = BudgetedLLM(llm, self.meter, enforce=False)

    async def run(self, task: Task) -> dict[str, Any]:
        cfg, engine = self.cfg, self.engine
        self._task = task
        self.agent = Agent(
            self._agent_llm,
            self._exec,
            task.eval,
            on_event=self._emit,
            max_tokens=cfg.max_tokens,
            tool_timeout=cfg.tool_timeout_s,
            system=SYSTEM_PROMPT.format(
                workdir=task.eval.workdir,
                ranking=ranking_text(task.eval),
                fork=(FORK_PROMPT + (FORCED_FORK_PROMPT if cfg.forced_fork else "")) if cfg.forking else "",
                rewind=REWIND_PROMPT if cfg.checkpoints else "",
                ranked="Branches are ranked" if cfg.forking or cfg.initial_alternatives else "Work is ranked",
            ),
            checkpointer=self._checkpoint if cfg.checkpoints else None,
            policy=task.checkpoint_policy if cfg.auto_checkpoint else None,
            forking=cfg.forking,
            forced_fork=cfg.forced_fork,
        )
        t0 = time.monotonic()
        run, root = await engine.start_run(task.prompt.strip().splitlines()[0][:200])
        self._run_id = run.id
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
            report["post_checks"] = await self._post_checks(result.branch.node_id, task.eval.workdir)
        except Exception as e:
            report.update(status="error", error=f"{type(e).__name__}: {e}")
            raise
        finally:
            report["fork_points"] = self._fork_points
            report["checkpoints"] = self._checkpoints
            report["rewinds"] = self._rewinds
            report["branches"] = [
                {
                    "node": b.node_id,
                    "name": b.name,
                    "model": b.model,
                    "steps": b.steps,
                    "tool_calls": b.tool_calls,
                    "rewinds": b.rewinds,
                    "stop": s.kind if s else None,
                    "summary": s.summary[:1000] if s else None,
                    "usage": b.usage.as_dict(),
                }
                for b, s in self._branches
            ]
            if cfg.transcripts:
                report["transcripts"] = {b.name or "root": _jsonable(b.messages) for b, _ in self._branches}
            report["usage_by_model"] = self._usage_by_model()
            report["cost"] = self.meter.as_dict()
            deleted = await engine.finish_run(run.id, keep_head=cfg.keep_head)
            report["tree"] = engine.render_tree(run.id)  # after teardown, so statuses are final
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
            if stop.kind == "rewind":
                try:
                    await self._rewind(br, stop)
                except (EngineError, BudgetExceeded, ValueError) as e:
                    self._emit(
                        {"type": "rewind_refused", "node": br.node_id, "branch": br.name, "reason": str(e)}
                    )
                    self.agent.resume_after_failed_rewind(br, stop, str(e))
                continue
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

        # The fork point itself is evaluated in a throwaway fork of the (now paused) parent, while the
        # branches run, so scores are relative to where the branches started.
        base_task = asyncio.create_task(self._baseline(br)) if self._eval_cfg.baseline else None
        try:
            results = await asyncio.gather(*(guarded(k) for k in kids))
        finally:
            baseline, base_deleted = await base_task if base_task else (None, [])
        sel = await select_winner(
            [r.evaluation for r in results],
            self._eval_cfg,
            baseline=baseline,
            llm=self._judge_llm,
            model=self.cfg.model,
            task=self._task.prompt if self._task else "",
        )
        wi = next(i for i, r in enumerate(results) if r.evaluation is sel.winner)
        winner = results[wi]
        deleted = base_deleted + await self.engine.promote(children[wi].id)
        if winner.branch.node_id != children[wi].id:  # the winner forked again; its own winner is deeper
            deleted += await self.engine.promote(winner.branch.node_id)
        why = sel.why
        artifacts = None
        if self.cfg.artifacts_dir:
            fp_name = f"fork{len(self._fork_points) + 1}-{br.name or 'root'}"
            artifacts = str(ArtifactStore(self.cfg.artifacts_dir).save_selection(self._run_id, fp_name, sel))
        self._fork_points.append(
            {
                "node": br.node_id,
                "branch": br.name,
                "fork_seconds": fork_s,
                "alternatives": alts,
                "children": [c.id for c in children],
                "evaluations": [r.evaluation.as_dict() for r in results],
                "baseline": baseline.as_dict() if baseline else None,
                "selection": sel.as_dict(),
                "winner": winner.branch.name,
                "winner_node": winner.branch.node_id,
                "deciding": sel.deciding,
                "why": why,
                "judge": sel.verdict.as_dict() if sel.verdict else None,
                "artifacts": artifacts,
                "deleted_nodes": deleted,
            }
        )
        self._emit(
            {
                "type": "promoted",
                "node": winner.branch.node_id,
                "branch": winner.branch.name,
                "why": why,
                "deciding": sel.deciding,
                "deleted": len(deleted),
            }
        )
        return winner

    async def _baseline(self, br: Branch) -> tuple[Evaluation | None, list[str]]:
        """Evaluate the fork point in a scratch fork of the paused parent, then delete the fork."""
        try:
            node = await self.engine.scratch_fork(br.node_id, "baseline (fork point)")
        except Exception as e:  # no baseline: scoring falls back to comparing branches with each other
            self._emit({"type": "baseline_skipped", "node": br.node_id, "branch": br.name, "reason": str(e)})
            return None, []
        ev = await evaluate(  # never raises for command or backend failures
            lambda c, t: self.engine.backend.exec(node.sandbox_id, c, timeout=t),
            self._eval_cfg,
            node_id=node.id,
            label="baseline",
        )
        self._emit(
            {
                "type": "baseline_evaluated",
                "node": node.id,
                "branch": br.name,
                "summary": ev.summary(),
                "seconds": round(ev.seconds, 2),
            }
        )
        try:
            deleted = await self.engine.discard(node.id)
        except Exception as e:  # finish_run retries LOST nodes
            log.warning("could not delete baseline node %s yet: %s", node.id, e)
            deleted = []
        return (ev if ev.error is None else None), deleted

    # checkpoints and rewind --------------------------------------------------------------------------
    async def _checkpoint(self, br: Branch, label: str, auto: bool) -> str:
        """Freeze the branch's node as a restore point and move the branch onto a fork of it."""
        if len(self._checkpoints) >= self.cfg.max_checkpoints:
            raise BudgetExceeded(f"this run allows at most {self.cfg.max_checkpoints} checkpoints")
        t0 = time.monotonic()
        cp, working = await self.engine.checkpoint(br.node_id, label)
        secs = round(time.monotonic() - t0, 3)
        br.node_id = working.id
        self._checkpoints.append(
            {
                "checkpoint": cp.id,
                "label": label,
                "auto": auto,
                "branch": br.name,
                "step": br.steps,
                "seconds": secs,
            }
        )
        self._emit(
            {
                "type": "checkpointed",
                "checkpoint": cp.id,
                "node": working.id,
                "branch": br.name,
                "auto": auto,
                "seconds": secs,
            }
        )
        return cp.id

    async def _rewind(self, br: Branch, stop: Stop) -> None:
        cp_id = stop.checkpoint_id or ""
        rec = br.find_checkpoint(cp_id)
        if rec is None:
            raise ValueError(f"{cp_id!r} is not a checkpoint on this branch's current timeline.")
        if br.rewinds >= self.cfg.max_rewinds:
            raise ValueError(f"this branch already rewound {br.rewinds} times (the limit).")
        discard = self._child_toward(cp_id, br.node_id)
        t0 = time.monotonic()
        fresh = await self.engine.rewind(cp_id, discard=discard, lesson=stop.lesson or None)
        secs = round(time.monotonic() - t0, 3)
        abandoned = br.node_id
        br.rewind_to(rec, node_id=fresh.id, lesson=stop.lesson)
        self._rewinds.append(
            {
                "branch": br.name,
                "checkpoint": cp_id,
                "label": rec.label,
                "auto_checkpoint": rec.auto,
                "abandoned_node": abandoned,
                "new_node": fresh.id,
                "lesson": stop.lesson,
                "step": br.steps,
                "seconds": secs,
            }
        )
        self._emit(
            {
                "type": "rewound",
                "branch": br.name,
                "checkpoint": cp_id,
                "node": fresh.id,
                "abandoned": abandoned,
                "seconds": secs,
            }
        )

    def _child_toward(self, ancestor_id: str, node_id: str) -> str:
        """The child of `ancestor_id` on the path down to `node_id` (the timeline a rewind abandons)."""
        node = self.engine.store.get_node(node_id)
        while node.parent_id is not None and node.parent_id != ancestor_id:
            node = self.engine.store.get_node(node.parent_id)
        if node.parent_id != ancestor_id:
            raise ValueError(f"checkpoint {ancestor_id} is not an ancestor of {node_id}")
        return node.id

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
        await seed_task(lambda c, t: self._exec(node_id, c, t), task)
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

    async def _post_checks(self, node_id: str, workdir: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, cmd in self.cfg.post_checks.items():
            try:
                res = await self._exec(node_id, in_dir(workdir, cmd), 300)
                out[name] = {"exit_code": res.exit_code, "output": res.output[-4000:]}
            except Exception as e:
                out[name] = {"exit_code": None, "output": f"{type(e).__name__}: {e}"}
        return out

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


FIXTURES_DIR = "/tmp/sfork-fixtures"


async def seed_task(exec_: Callable[[str, int], Awaitable[Any]], task: Task) -> None:
    """Write the task's repo and fixtures into a fresh sandbox, commit + tag the baseline, run setup."""
    wd = task.eval.workdir
    res = await exec_(f"mkdir -p {wd}", 60)
    if not res.ok:
        raise EngineError(f"could not create {wd}: {res.output[-500:]}")
    for rel, content in task.files.items():
        res = await write_file(exec_, f"{wd}/{rel}", content)
        if not res.ok:
            raise EngineError(f"could not write {rel}: {res.output[-500:]}")
    if task.executable:
        paths = " ".join(shlex.quote(f"{wd}/{rel}") for rel in task.executable)
        await exec_(f"chmod +x {paths}", 60)
    # A baseline commit makes the deliverable a plain `git diff`, and the evaluator measures each
    # branch's change against its tag. Skipped if git is missing.
    await exec_(
        in_dir(
            wd,
            f"command -v git >/dev/null && git init -q && git add -A && "
            f"{_GIT} commit -qm baseline && git tag -f {BASE_TAG} >/dev/null || true",
        ),
        120,
    )
    for rel, content in task.fixtures.items():
        res = await write_file(exec_, f"{FIXTURES_DIR}/{rel}", content)
        if not res.ok:
            raise EngineError(f"could not write fixture {rel}: {res.output[-500:]}")
    for cmd in task.eval.setup:
        res = await exec_(in_dir(wd, cmd), 900)
        if not res.ok:
            raise EngineError(f"setup command failed ({cmd}): {res.output[-1000:]}")


log = logging.getLogger(__name__)


def ranking_text(cfg: EvalConfig) -> str:
    """How branches are ranked, in words, for the agent's system prompt."""
    dq = ["all gates pass"]
    if cfg.quality.protected:
        dq.append(f"no edits to {', '.join(cfg.quality.protected)}")
    if cfg.tests is not None and cfg.tests.regressions == "gate":
        dq.append("no test that passed at the fork point may fail")
    parts = [" and ".join(dq) + " (otherwise the branch is disqualified)"]
    if cfg.tests is not None and cfg.correctness_first:
        parts.append("most tests passing")
    extra = []
    if cfg.benchmarks:
        extra.append(
            "benchmarks relative to the fork point ("
            + ", ".join(f"{b.name}, {b.direction} is better" for b in cfg.benchmarks)
            + ")"
        )
    if cfg.quality.measure_diff:
        extra.append("a small, focused diff")
    if cfg.quality.lint is not None:
        extra.append("no new lint findings")
    if extra:
        parts.append("a weighted score of " + ", ".join(extra))
    return ", then ".join(parts)


def _jsonable(x: Any) -> Any:
    """SDK content blocks (pydantic models or SimpleNamespace) to plain JSON data."""
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_jsonable(v) for v in x]
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json", exclude_none=True)
    if hasattr(x, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(x).items()}
    return x


def _read_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        f.relative_to(root).as_posix(): f.read_text()
        for f in sorted(root.rglob("*"))
        if f.is_file() and "__pycache__" not in f.parts
    }


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
