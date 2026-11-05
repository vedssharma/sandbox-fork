"""The tree engine: fork, checkpoint, rewind, promote, garbage-collect, compact (PLAN.md section 4).

No agent and no evaluator yet. This is the Phase 1 core that the CLI drives by hand and that the
agent runtime (Phase 3) will call through tools.

Invariants
- A node with status RUNNING owns a started sandbox; PAUSED owns a paused one; GC owns none.
- Forking a node pauses it: it becomes the restore point and its children carry the work. This keeps
  the tree deletable from the leaves inward, which Daytona requires (a sandbox with live fork
  children can't be deleted).
- Checkpoints are never mutated. Rewinding forks a fresh child from the checkpoint, so you can rewind
  to the same point any number of times.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from ..core.budget import Budget
from ..core.models import LIVE_WORK_STATUSES, Node, NodeKind, NodeStatus, Run, RunStatus
from ..core.store import TreeStore
from ..daytona.base import BackendError, ExecResult, ParentHasChildrenError, SandboxBackend
from ..guest import post_fork_command

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


class TreeEngine:
    def __init__(
        self,
        store: TreeStore,
        backend: SandboxBackend,
        *,
        budget: Budget | None = None,
        fork_concurrency: int = 4,
        run_post_fork_hook: bool = True,
    ) -> None:
        self.store = store
        self.backend = backend
        self.budget = budget or Budget()
        self._fork_sem = asyncio.Semaphore(fork_concurrency)
        self._run_hook = run_post_fork_hook

    # runs -----------------------------------------------------------------
    async def start_run(self, task: str, *, snapshot: str | None = None) -> tuple[Run, Node]:
        run = Run(task=task)
        sandbox_id = await self.backend.create(name=f"sfork-{run.id}-root", snapshot=snapshot)
        root = Node(run_id=run.id, sandbox_id=sandbox_id, kind=NodeKind.ROOT)
        run.head_node_id = root.id
        self.store.put_run(run)
        self.store.put_node(root)
        return run, root

    async def finish_run(self, run_id: str, *, keep_head: bool = False) -> list[str]:
        """Delete every sandbox in the run (optionally keeping the head and its ancestors alive)."""
        run = self.store.get_run(run_id)
        for node in self.store.run_nodes(run_id):
            if node.status is NodeStatus.GC or (keep_head and node.id == run.head_node_id):
                continue
            self._set_status(node, NodeStatus.LOST)
        deleted = await self.gc(run_id)
        run.status = RunStatus.FINISHED
        self.store.put_run(run)
        return deleted

    # tree operations ------------------------------------------------------
    async def fork(self, node_id: str, hypotheses: list[str]) -> list[Node]:
        """Fork a running node into one child per hypothesis; the node becomes a paused restore point."""
        parent = self._require(node_id, NodeStatus.RUNNING)
        self.budget.check_fork(
            running_sandboxes=self._running_count(parent.run_id),
            parent_depth=parent.depth,
            n=len(hypotheses),
        )
        children = await self._fork_children(parent, hypotheses, kind=NodeKind.BRANCH)
        await self._pause_as_checkpoint(parent)
        return children

    async def checkpoint(self, node_id: str, label: str) -> tuple[Node, Node]:
        """Freeze a running node as a labeled restore point and continue work in a fork of it.

        Returns (checkpoint_node, working_node). Callers must switch to the working node.
        """
        node = self._require(node_id, NodeStatus.RUNNING)
        self.budget.check_fork(
            running_sandboxes=self._running_count(node.run_id), parent_depth=node.depth, n=1
        )
        (working,) = await self._fork_children(node, [node.hypothesis], kind=NodeKind.BRANCH)
        node.label = label
        await self._pause_as_checkpoint(node)
        self._move_head(node.run_id, from_id=node.id, to_id=working.id)
        return node, working

    async def rewind(
        self, checkpoint_id: str, *, discard: str | None = None, lesson: str | None = None
    ) -> Node:
        """Start a fresh live node from a checkpoint, optionally discarding the node being abandoned.

        The checkpoint is left untouched so it can be rewound to again.
        """
        cp = self._require(checkpoint_id, NodeStatus.PAUSED)
        self.budget.check_fork(running_sandboxes=self._running_count(cp.run_id), parent_depth=cp.depth, n=1)
        (fresh,) = await self._fork_children(cp, [cp.hypothesis], kind=NodeKind.BRANCH)
        if lesson:
            fresh.meta["lesson"] = lesson
            self.store.put_node(fresh)
        run = self.store.get_run(cp.run_id)
        run.head_node_id = fresh.id
        self.store.put_run(run)
        if discard:
            self._mark_subtree_lost(discard)
            await self.gc(cp.run_id)
        return fresh

    async def promote(self, node_id: str) -> list[str]:
        """Make a node the run's head and delete its competing siblings (and their subtrees)."""
        winner = self.store.get_node(node_id)
        if winner.status not in LIVE_WORK_STATUSES | {NodeStatus.PAUSED}:
            raise EngineError(f"cannot promote node in status {winner.status}")
        run = self.store.get_run(winner.run_id)
        run.head_node_id = winner.id
        self.store.put_run(run)
        if winner.parent_id:
            for sib in self.store.children(winner.parent_id):
                if sib.id != winner.id and sib.status is not NodeStatus.GC:
                    self._mark_subtree_lost(sib.id)
        return await self.gc(winner.run_id)

    async def exec(self, node_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        node = self._require(node_id, NodeStatus.RUNNING)
        return await self.backend.exec(node.sandbox_id, command, timeout=timeout)

    async def gc(self, run_id: str) -> list[str]:
        """Delete LOST nodes from the leaves inward. Returns the ids of nodes whose sandbox was deleted.

        A LOST node whose descendants are still alive (e.g. the ancestors of a kept head) stays LOST
        and is retried on the next gc.
        """
        deleted: list[str] = []
        progress = True
        while progress:
            progress = False
            for node in sorted(self.store.run_nodes(run_id), key=lambda n: -n.depth):
                if node.status is not NodeStatus.LOST:
                    continue
                if any(c.status is not NodeStatus.GC for c in self.store.children(node.id)):
                    continue
                try:
                    await self.backend.delete(node.sandbox_id)
                except ParentHasChildrenError:
                    log.warning("gc: %s still has live forks on the backend; will retry", node.id)
                    continue
                self._set_status(node, NodeStatus.GC)
                deleted.append(node.id)
                progress = True
        return deleted

    async def compact(self, run_id: str) -> Node:
        """Replace the head's ancestor chain with a fresh root made from a hot snapshot of the head."""
        run = self.store.get_run(run_id)
        head = self._require(run.head_node_id or "", NodeStatus.RUNNING)
        snap = await self.backend.hot_snapshot(head.sandbox_id, f"sfork-{run_id}-{int(time.time())}")
        sandbox_id = await self.backend.create(name=f"sfork-{run_id}-root-c", snapshot=snap)
        root = Node(
            run_id=run_id,
            sandbox_id=sandbox_id,
            kind=NodeKind.ROOT,
            hypothesis=head.hypothesis,
            meta={"compacted_from": head.id, "snapshot": snap},
        )
        self.store.put_node(root)
        for node in self.store.run_nodes(run_id):
            if node.id != root.id and node.status is not NodeStatus.GC:
                self._set_status(node, NodeStatus.LOST)
        run.head_node_id = root.id
        self.store.put_run(run)
        await self.gc(run_id)
        return root

    # views ----------------------------------------------------------------
    def render_tree(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        nodes = self.store.run_nodes(run_id)
        roots = [n for n in nodes if n.parent_id is None]
        lines = [f"{run.id}  [{run.status}]  {run.task}"]

        def walk(n: Node, prefix: str, last: bool) -> None:
            head = " <- head" if n.id == run.head_node_id else ""
            desc = n.label or n.hypothesis or ""
            lines.append(
                f"{prefix}{'└── ' if last else '├── '}{n.id} {n.kind}/{n.status}"
                f"{' ' + repr(desc) if desc else ''}{head}"
            )
            kids = self.store.children(n.id)
            for i, k in enumerate(kids):
                walk(k, prefix + ("    " if last else "│   "), i == len(kids) - 1)

        for i, r in enumerate(roots):
            walk(r, "", i == len(roots) - 1)
        return "\n".join(lines)

    # internals ------------------------------------------------------------
    def _require(self, node_id: str, status: NodeStatus) -> Node:
        try:
            node = self.store.get_node(node_id)
        except KeyError:
            raise EngineError(f"no such node {node_id!r}") from None
        if node.status is not status:
            raise EngineError(f"node {node_id} is {node.status}, expected {status}")
        return node

    def _running_count(self, run_id: str) -> int:
        return sum(1 for n in self.store.run_nodes(run_id) if n.status in LIVE_WORK_STATUSES)

    def _set_status(self, node: Node, status: NodeStatus) -> None:
        node.status = status
        self.store.put_node(node)

    def _move_head(self, run_id: str, *, from_id: str, to_id: str) -> None:
        run = self.store.get_run(run_id)
        if run.head_node_id == from_id:
            run.head_node_id = to_id
            self.store.put_run(run)

    def _mark_subtree_lost(self, node_id: str) -> None:
        stack = [node_id]
        while stack:
            n = self.store.get_node(stack.pop())
            if n.status is not NodeStatus.GC:
                self._set_status(n, NodeStatus.LOST)
            stack.extend(c.id for c in self.store.children(n.id))

    async def _pause_as_checkpoint(self, node: Node) -> None:
        # Note: we fork first and pause after, so the parent runs briefly past the fork instant.
        # Phase 0 checks whether forking an already-paused VM is supported; if so, flip the order.
        await self.backend.pause(node.sandbox_id)
        node.status = NodeStatus.PAUSED
        if node.kind is not NodeKind.ROOT:
            node.kind = NodeKind.CHECKPOINT
        self.store.put_node(node)

    async def _fork_children(
        self, parent: Node, hypotheses: Iterable[str | None], *, kind: NodeKind
    ) -> list[Node]:
        hyps = list(hypotheses)
        # Backends that can make N children from one checkpoint (E2B) get a single batched call, so
        # the parent pauses once instead of N times and every child sees the same instant.
        batch: list[str] = []
        batch_s = 0.0
        fork_many = getattr(self.backend, "fork_many", None)
        if fork_many is not None and len(hyps) > 1:
            t0 = time.monotonic()
            try:
                batch = await fork_many(parent.sandbox_id, len(hyps))
            except BackendError as e:  # fork_many cleans up its own partial children
                raise EngineError(f"fork of {parent.id} failed: {e}") from e
            batch_s = time.monotonic() - t0

        async def one(i: int, hyp: str | None) -> Node:
            if batch:
                sid, fork_s = batch[i], batch_s
            else:
                async with self._fork_sem:
                    t0 = time.monotonic()
                    sid = await self.backend.fork(parent.sandbox_id, name=f"sfork-{parent.id}-{i}")
                    fork_s = time.monotonic() - t0
            child = Node(
                run_id=parent.run_id,
                sandbox_id=sid,
                kind=kind,
                parent_id=parent.id,
                depth=parent.depth + 1,
                hypothesis=hyp,
                meta={"fork_seconds": round(fork_s, 3)},
            )
            self.store.put_node(child)
            if self._run_hook:
                res = await self.backend.exec(sid, post_fork_command(hostname=child.id[-12:]), timeout=60)
                if not res.ok or "ready" not in res.output:
                    child.meta["post_fork_hook_error"] = res.output[-500:]
                    self._set_status(child, NodeStatus.FAILED)
            return child

        results = await asyncio.gather(*(one(i, h) for i, h in enumerate(hyps)), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        made = [r for r in results if isinstance(r, Node)]
        if errors:
            for child in made:
                self._mark_subtree_lost(child.id)
            await self.gc(parent.run_id)
            raise EngineError(f"fork of {parent.id} failed: {errors[0]}") from errors[0]
        failed = [c for c in made if c.status is NodeStatus.FAILED]
        if failed:
            for child in made:
                self._mark_subtree_lost(child.id)
            await self.gc(parent.run_id)
            raise EngineError(f"post-fork hook failed in {failed[0].id}: {failed[0].meta}")
        return made


__all__ = ["BackendError", "EngineError", "TreeEngine"]
