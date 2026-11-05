import asyncio

import pytest

from sfork.core.budget import Budget, BudgetExceeded
from sfork.core.models import NodeKind, NodeStatus
from sfork.core.store import TreeStore
from sfork.daytona import FakeBackend, ParentHasChildrenError
from sfork.orchestrator.engine import EngineError, TreeEngine


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env():
    backend = FakeBackend()
    engine = TreeEngine(TreeStore(), backend, budget=Budget(max_running_sandboxes=8))
    return engine, backend


def files(backend: FakeBackend, node) -> dict[str, str]:
    return backend.sandboxes[node.sandbox_id].files


def test_fork_copies_state_and_pauses_parent(env):
    engine, backend = env

    async def go():
        _, root = await engine.start_run("demo")
        await engine.exec(root.id, "echo v1 > /app/schema")
        a, b = await engine.fork(root.id, ["pattern A", "pattern B"])
        return root, a, b

    root, a, b = run(go())
    root = engine.store.get_node(root.id)
    assert root.status is NodeStatus.PAUSED and root.kind is NodeKind.ROOT
    assert backend.sandboxes[root.sandbox_id].state == "paused"
    for child in (a, b):
        assert child.parent_id == root.id and child.depth == 1
        assert files(backend, child)["/app/schema"] == "v1\n"
    # post-fork hook regenerated machine-id in each child
    ids = {files(backend, n)["/etc/machine-id"] for n in (root, a, b)}
    assert len(ids) == 3


def test_branches_are_isolated_and_promote_deletes_losers(env):
    engine, backend = env

    async def go():
        r, root = await engine.start_run("migrate")
        await engine.exec(root.id, "echo users(id) > /db/schema")
        good, bad = await engine.fork(root.id, ["additive migration", "destructive migration"])
        await engine.exec(good.id, "echo users(id,email) > /db/schema")
        await engine.exec(bad.id, "rm /db/schema")
        assert (await engine.exec(bad.id, "cat /db/schema")).exit_code == 1
        assert (await engine.exec(good.id, "cat /db/schema")).output == "users(id,email)\n"
        deleted = await engine.promote(good.id)
        return r, root, good, bad, deleted

    r, root, good, bad, deleted = run(go())
    assert deleted == [bad.id]
    assert backend.sandboxes[bad.sandbox_id].state == "destroyed"
    assert engine.store.get_run(r.id).head_node_id == good.id
    assert engine.store.get_node(root.id).status is NodeStatus.PAUSED  # kept as restore point


def test_checkpoint_and_rewind_leave_no_leftover_state(env):
    """Phase 1 exit criterion: break a migration, rewind, and see none of it."""
    engine, backend = env

    async def go():
        r, root = await engine.start_run("migrate")
        await engine.exec(root.id, "echo users(id) > /db/schema")
        cp, work = await engine.checkpoint(root.id, "before migration")
        await engine.exec(work.id, "rm /db/schema")
        await engine.exec(work.id, "echo junk > /tmp/half-applied")
        fresh = await engine.rewind(cp.id, discard=work.id, lesson="dropping the table loses data")
        # rewinding twice to the same checkpoint works and also yields the clean state
        again = await engine.rewind(cp.id)
        return r, cp, work, fresh, again

    r, cp, work, fresh, again = run(go())
    for n in (fresh, again):
        f = files(backend, n)
        assert f["/db/schema"] == "users(id)\n"
        assert "/tmp/half-applied" not in f
    assert backend.sandboxes[work.sandbox_id].state == "destroyed"
    assert engine.store.get_node(work.id).status is NodeStatus.GC
    assert engine.store.get_node(cp.id).label == "before migration"
    assert engine.store.get_node(fresh.id).meta["lesson"] == "dropping the table loses data"
    assert engine.store.get_run(r.id).head_node_id == again.id


def test_gc_deletes_leaves_first(env):
    engine, backend = env

    async def go():
        r, root = await engine.start_run("t")
        (a,) = await engine.fork(root.id, ["a"])
        (a1, a2) = await engine.fork(a.id, ["a1", "a2"])
        return r, root, a, a1, a2

    r, root, a, a1, a2 = run(go())
    # the backend itself enforces the Daytona rule
    with pytest.raises(ParentHasChildrenError):
        run(backend.delete(a.sandbox_id))
    deleted = run(engine.finish_run(r.id))
    assert deleted.index(a1.id) < deleted.index(a.id) < deleted.index(root.id)
    assert deleted.index(a2.id) < deleted.index(a.id)
    assert backend.live_ids() == set()


def test_finish_run_keep_head_keeps_ancestors(env):
    engine, backend = env

    async def go():
        r, root = await engine.start_run("t")
        (a, b) = await engine.fork(root.id, ["a", "b"])
        await engine.promote(a.id)
        await engine.finish_run(r.id, keep_head=True)
        return root, a, b

    root, a, b = run(go())
    assert backend.sandboxes[a.sandbox_id].state == "started"
    assert backend.sandboxes[b.sandbox_id].state == "destroyed"
    # root can't be deleted while the kept head is its live fork child
    assert engine.store.get_node(root.id).status is NodeStatus.LOST
    assert backend.sandboxes[root.sandbox_id].state == "paused"


def test_compact_replaces_chain_with_fresh_root(env):
    engine, backend = env

    async def go():
        r, root = await engine.start_run("t")
        await engine.exec(root.id, "echo hello > /f")
        (a,) = await engine.fork(root.id, ["a"])
        (b,) = await engine.fork(a.id, ["b"])
        await engine.exec(b.id, "echo world >> /f")
        await engine.promote(b.id)  # head stays at the fork point until something is promoted
        new_root = await engine.compact(r.id)
        return r, new_root

    r, new_root = run(go())
    assert new_root.parent_id is None and new_root.depth == 0
    assert files(backend, new_root)["/f"] == "hello\nworld\n"
    assert backend.live_ids() == {new_root.sandbox_id}
    assert engine.store.get_run(r.id).head_node_id == new_root.id


def test_budget_limits_fanout_and_running(env):
    engine, _ = env
    small = TreeEngine(TreeStore(), FakeBackend(), budget=Budget(max_fanout=2, max_running_sandboxes=3))

    async def go():
        _, root = await small.start_run("t")
        with pytest.raises(BudgetExceeded):
            await small.fork(root.id, ["a", "b", "c"])
        a, b = await small.fork(root.id, ["a", "b"])
        with pytest.raises(BudgetExceeded):
            await small.fork(a.id, ["a1", "a2"])  # 2 running + 2 new > 3

    run(go())


def test_fork_failure_cleans_up_partial_children():
    class Flaky(FakeBackend):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def fork(self, sandbox_id, *, name=None):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("429 Too Many Requests")
            return await super().fork(sandbox_id, name=name)

    backend = Flaky()
    engine = TreeEngine(TreeStore(), backend, fork_concurrency=1)

    async def go():
        _, root = await engine.start_run("t")
        with pytest.raises(EngineError):
            await engine.fork(root.id, ["a", "b", "c"])
        return root

    root = run(go())
    assert backend.live_ids() == {root.sandbox_id}
    assert engine.store.get_node(root.id).status is NodeStatus.RUNNING  # parent untouched


def test_operations_validate_status(env):
    engine, _ = env

    async def go():
        _, root = await engine.start_run("t")
        await engine.fork(root.id, ["a"])
        with pytest.raises(EngineError):
            await engine.exec(root.id, "true")  # paused
        with pytest.raises(EngineError):
            await engine.rewind("node_missing")

    run(go())


def test_render_tree(env):
    engine, _ = env

    async def go():
        r, root = await engine.start_run("demo task")
        a, b = await engine.fork(root.id, ["A", "B"])
        await engine.promote(a.id)
        return r

    r = run(go())
    out = engine.render_tree(r.id)
    assert "demo task" in out and "'A' <- head" in out and "branch/gc 'B'" in out
