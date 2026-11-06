"""MorphBackend against a stub of the morphcloud client (no network)."""

import asyncio
import itertools
from types import SimpleNamespace

import pytest

from sfork.core.store import TreeStore
from sfork.daytona import BackendError, SandboxBackend
from sfork.daytona.morph import MorphBackend
from sfork.orchestrator.engine import TreeEngine


class ApiError(Exception):
    def __init__(self, status_code, msg="api error"):
        super().__init__(msg)
        self.status_code = status_code


class Cloud:
    """Shared state for the stub: instances, snapshots and a call log."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.instances: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.short_branch = False
        self.throttle = 0

    def new_snapshot(self, files, digest=None):
        sid = f"snap{next(self.ids)}"
        self.snapshots[sid] = {"files": dict(files), "digest": digest}
        return StubSnapshot(self, sid)

    def new_instance(self, files):
        iid = f"inst{next(self.ids)}"
        self.instances[iid] = {"status": "ready", "files": dict(files)}
        return StubInstance(self, iid)


class StubSnapshot:
    def __init__(self, cloud, sid):
        self._c, self.id = cloud, sid

    async def await_until_ready(self, timeout=None):
        pass

    async def adelete(self):
        self._c.calls.append(("snapshot_delete", self.id))
        if self._c.snapshots.pop(self.id, None) is None:
            raise ApiError(404)


class StubInstance:
    def __init__(self, cloud, iid):
        self._c, self.id = cloud, iid

    @property
    def status(self):
        return self._c.instances[self.id]["status"]

    @property
    def _files(self):
        return self._c.instances[self.id]["files"]

    async def abranch(self, count):
        self._c.calls.append(("branch", self.id, count))
        if self.status != "ready":
            raise ApiError(409, f"instance is {self.status}")
        snap = self._c.new_snapshot(self._files)
        n = count - 1 if self._c.short_branch else count
        return snap, [self._c.new_instance(self._files) for _ in range(n)]

    async def apause(self):
        self._c.instances[self.id]["status"] = "paused"

    async def aresume(self):
        self._c.instances[self.id]["status"] = "ready"

    async def await_until_ready(self, timeout=None):
        pass

    async def asnapshot(self, digest=None, metadata=None, ttl_seconds=None):
        self._c.calls.append(("snapshot", self.id, metadata))
        return self._c.new_snapshot(self._files)

    async def aexec(self, command, timeout=None):
        if self._c.throttle:
            self._c.throttle -= 1
            raise ApiError(429, "slow down")
        assert command[:2] == ["sh", "-c"]
        cmd = command[2]
        if cmd.startswith("write "):
            _, path, val = cmd.split(" ", 2)
            self._files[path] = val
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        if cmd.startswith("read "):
            return SimpleNamespace(exit_code=0, stdout=self._files.get(cmd[5:], ""), stderr="")
        if "sfork-guest post-fork" in cmd:
            return SimpleNamespace(exit_code=0, stdout="ready\n", stderr="")
        return SimpleNamespace(exit_code=3, stdout="", stderr="boom")


class StubClient:
    def __init__(self, cloud):
        c = cloud

        class Instances:
            async def astart(
                self, snapshot_id, metadata=None, ttl_seconds=None, ttl_action=None, timeout=None
            ):
                c.calls.append(("start", snapshot_id, ttl_seconds, ttl_action))
                return c.new_instance(c.snapshots[snapshot_id]["files"])

            async def aget(self, instance_id):
                if instance_id not in c.instances:
                    raise ApiError(404, "not found")
                return StubInstance(c, instance_id)

            async def astop(self, instance_id):
                c.instances.pop(instance_id, None)

        class Snapshots:
            async def alist(self, digest=None, metadata=None):
                return [StubSnapshot(c, k) for k, v in c.snapshots.items() if v["digest"] == digest]

            async def acreate(self, image_id=None, vcpus=None, memory=None, disk_size=None, digest=None, **_):
                c.calls.append(("base", image_id, vcpus, memory, disk_size))
                return c.new_snapshot({}, digest=digest)

            async def aget(self, snapshot_id):
                if snapshot_id not in c.snapshots:
                    raise ApiError(404)
                return StubSnapshot(c, snapshot_id)

        self.instances, self.snapshots = Instances(), Snapshots()


@pytest.fixture
def cloud():
    return Cloud()


@pytest.fixture
def be(cloud, monkeypatch):
    monkeypatch.setattr("sfork.daytona.morph.random.random", lambda: 0.0)
    return MorphBackend(client=StubClient(cloud), ttl_seconds=900, max_retries=2)


def run(coro):
    return asyncio.run(coro)


def test_is_a_sandbox_backend(be):
    assert isinstance(be, SandboxBackend)


def test_base_snapshot_is_built_once_and_reused(cloud):
    async def go():
        a = MorphBackend(client=StubClient(cloud))
        await a.create()
        await a.create()
        b = MorphBackend(client=StubClient(cloud))  # a later run finds it by digest
        await b.create()
        assert [c for c in cloud.calls if c[0] == "base"] == [("base", "morphvm-minimal", 2, 2048, 8192)]

    run(go())


def test_create_exec_fork_isolation(be, cloud):
    async def go():
        root = await be.create()
        assert ("start", next(iter(cloud.snapshots)), 900, "stop") in cloud.calls
        await be.exec(root, "write /a parent")
        child = await be.fork(root)
        await be.exec(child, "write /a child")
        assert (await be.exec(root, "read /a")).output == "parent"
        assert (await be.exec(child, "read /a")).output == "child"
        bad = await be.exec(root, "nope")
        assert (bad.exit_code, bad.output) == (3, "boom")
        assert await be.state(root) == "started"
        await be.delete(root)  # Morph children don't pin their parent
        assert await be.state(root) == "destroyed"
        assert await be.state(child) == "started"

    run(go())


def test_fork_many_is_one_branch_call(be, cloud):
    async def go():
        root = await be.create()
        kids = await be.fork_many(root, 4)
        assert len(set(kids)) == 4
        assert [c for c in cloud.calls if c[0] == "branch"] == [("branch", root, 4)]

    run(go())


def test_branch_snapshot_is_deleted_with_its_last_child(be, cloud):
    async def go():
        root = await be.create()
        before = set(cloud.snapshots)
        a, b = await be.fork_many(root, 2)
        (branch_snap,) = set(cloud.snapshots) - before
        await be.delete(a)
        assert branch_snap in cloud.snapshots
        await be.delete(b)
        assert branch_snap not in cloud.snapshots

    run(go())


def test_fork_of_paused_checkpoint_resumes_then_repauses(be):
    async def go():
        root = await be.create()
        await be.pause(root)
        assert await be.state(root) == "paused"
        child = await be.fork(root)
        assert await be.state(child) == "started"
        assert await be.state(root) == "paused"

    run(go())


def test_raw_fork_of_paused_parent_is_rejected_without_auto_resume(be):
    async def go():
        root = await be.create()
        await be.pause(root)
        be.auto_resume_paused = False
        with pytest.raises(BackendError, match="paused"):
            await be.fork(root)

    run(go())


def test_short_branch_cleans_up_and_raises(be, cloud):
    async def go():
        root = await be.create()
        cloud.short_branch = True
        with pytest.raises(BackendError, match="asked for 3"):
            await be.fork_many(root, 3)
        assert set(cloud.instances) == {root}

    run(go())


def test_concurrent_forks_of_one_parent_are_serialized(be):
    async def go():
        root = await be.create()
        kids = await asyncio.gather(be.fork(root), be.fork(root), be.fork_many(root, 2))
        assert len({kids[0], kids[1], *kids[2]}) == 4

    run(go())


def test_rate_limit_is_retried(be, cloud, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("sfork.daytona.morph.asyncio.sleep", no_sleep)

    async def go():
        root = await be.create()
        cloud.throttle = 2
        assert (await be.exec(root, "write /x 1")).ok
        cloud.throttle = 5
        with pytest.raises(BackendError, match="slow down"):
            await be.exec(root, "read /x")

    run(go())


def test_hot_snapshot_roundtrip(be, cloud):
    async def go():
        root = await be.create()
        await be.exec(root, "write /db v1")
        snap = await be.hot_snapshot(root, "cp")
        assert cloud.calls[-1][2]["name"] == "cp"
        restored = await be.create(snapshot=snap)
        assert (await be.exec(restored, "read /db")).output == "v1"
        await be.delete_snapshot(snap)
        assert snap not in cloud.snapshots

    run(go())


def test_engine_batches_forks_through_one_branch(be, cloud, tmp_path):
    async def go():
        engine = TreeEngine(TreeStore(tmp_path / "t.db"), be)
        _, root = await engine.start_run("task")
        kids = await engine.fork(root.id, ["a", "b", "c"])
        assert len(kids) == 3
        assert [c for c in cloud.calls if c[0] == "branch"] == [("branch", root.sandbox_id, 3)]

    run(go())
