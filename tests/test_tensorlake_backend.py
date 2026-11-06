"""TensorlakeBackend against a stub of tensorlake's AsyncSandboxClient (no network)."""

import asyncio
import itertools
from types import SimpleNamespace

import pytest

pytest.importorskip("tensorlake")

from sfork.core.store import TreeStore  # noqa: E402
from sfork.daytona import BackendError, SandboxBackend, make_backend  # noqa: E402
from sfork.daytona.tensorlake import TensorlakeBackend  # noqa: E402
from sfork.orchestrator.engine import TreeEngine  # noqa: E402


class SandboxNotFoundError(Exception):
    pass


class RemoteAPIError(Exception):
    def __init__(self, status_code, msg):
        super().__init__(msg)
        self.status_code = status_code


class StubConn:
    def __init__(self, client, sid):
        self._c, self.sid = client, sid
        self.closed = False

    async def run(self, command, args=None, timeout=None, user=None):
        box = self._c.boxes[self.sid]
        if box["status"] != "running" or self.closed:
            raise RemoteAPIError(409, f"sandbox is {box['status']}")
        assert command == "sh" and args[0] == "-c"
        cmd, files = args[1], box["files"]
        if cmd.startswith("write "):
            _, path, val = cmd.split(" ", 2)
            files[path] = val
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        if cmd.startswith("read "):
            return SimpleNamespace(exit_code=0, stdout=files.get(cmd[5:], ""), stderr="")
        if "sfork-guest post-fork" in cmd:
            return SimpleNamespace(exit_code=0, stdout="ready\n", stderr="")
        return SimpleNamespace(exit_code=3, stdout="", stderr="boom")

    async def close(self):
        self.closed = True


class StubClient:
    def __init__(self):
        self.ids = itertools.count(1)
        self.boxes: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_copy_index: int | None = None
        self.unnamed_copies = False

    def _new(self, files, name):
        sid = f"sb{next(self.ids)}"
        self.boxes[sid] = {"status": "running", "files": dict(files), "name": name}
        return sid

    async def create(
        self, image=None, cpus=None, memory_mb=None, timeout_secs=None, snapshot_id=None, name=None
    ):
        self.calls.append(("create", image, snapshot_id, timeout_secs, name))
        files = self.snapshots[snapshot_id]["files"] if snapshot_id else {}
        return SimpleNamespace(sandbox_id=self._new(files, name), status="running")

    async def connect(self, sandbox_id):
        if self.boxes[sandbox_id]["status"] != "running":
            raise RemoteAPIError(409, "not running")
        return StubConn(self, sandbox_id)

    async def copy(self, sandbox_id, times=1):
        self.calls.append(("copy", sandbox_id, times))
        src = self.boxes[sandbox_id]
        if src["status"] not in ("running", "suspended"):
            raise RemoteAPIError(400, f"cannot copy a {src['status']} sandbox")
        if src["name"] and not self.unnamed_copies:
            live = {b["name"] for b in self.boxes.values()}
            for i in range(times):
                if self._copy_name(src["name"], times, i) in live:
                    raise RemoteAPIError(
                        409, f"sandbox name '{self._copy_name(src['name'], times, i)}' is already claimed"
                    )
        out = []
        for i in range(times):
            if self.fail_copy_index == i:
                out.append(
                    SimpleNamespace(sandbox_id=f"bad{i}", status="failed", reason="no_capacity", name=None)
                )
                continue
            name = None if self.unnamed_copies or not src["name"] else self._copy_name(src["name"], times, i)
            child = self._new(src["files"], name)
            self.snapshots[f"copy-{sandbox_id}-{child}"] = {"files": {}, "source": sandbox_id, "pins": set()}
            if src["status"] == "suspended":
                self.snapshots[src["suspend_snap"]]["pins"].add(child)
            out.append(SimpleNamespace(sandbox_id=child, status="running", name=name))
        return SimpleNamespace(source_sandbox_id=sandbox_id, sandboxes=out)

    @staticmethod
    def _copy_name(name, times, i):
        # The real server: "<name>-copy" for one copy, "<name>-copy-1..N" for a batch.
        return f"{name}-copy" if times == 1 else f"{name}-copy-{i + 1}"

    async def update_sandbox(self, sandbox_id, name=None):
        raise RemoteAPIError(400, "Sandbox names are immutable; set name when creating the sandbox")

    async def suspend(self, sandbox_id):
        box = self.boxes[sandbox_id]
        if not box["name"]:
            raise RemoteAPIError(400, "ephemeral sandboxes cannot be suspended")
        box["status"] = "suspended"
        snap = f"suspend-{sandbox_id}-{next(self.ids)}"
        box["suspend_snap"] = snap
        self.snapshots[snap] = {"files": {}, "source": sandbox_id, "pins": {sandbox_id}}

    async def resume(self, sandbox_id):
        self.boxes[sandbox_id]["status"] = "running"

    async def snapshot_and_wait(self, sandbox_id, snapshot_type=None):
        self.calls.append(("snapshot", sandbox_id, str(getattr(snapshot_type, "value", snapshot_type))))
        if self.boxes[sandbox_id]["status"] != "running":
            raise RemoteAPIError(404, "Snapshot not found")
        sid = f"snap{next(self.ids)}"
        self.snapshots[sid] = {"files": dict(self.boxes[sandbox_id]["files"])}
        return SimpleNamespace(snapshot_id=sid)

    async def delete_snapshot(self, snapshot_id):
        live = [p for p in self.snapshots[snapshot_id].get("pins", ()) if p in self.boxes]
        if live:
            raise RemoteAPIError(409, f"Snapshot is referenced by live sandbox '{live[0]}'")
        self.snapshots.pop(snapshot_id)

    async def list_sandbox_snapshots(self, sandbox_id):
        return [
            SimpleNamespace(snapshot_id=k) for k, v in self.snapshots.items() if v.get("source") == sandbox_id
        ]

    async def delete(self, sandbox_id):
        self.boxes.pop(sandbox_id, None)

    async def get(self, sandbox_id):
        if sandbox_id not in self.boxes:
            raise SandboxNotFoundError(sandbox_id)
        return SimpleNamespace(status=self.boxes[sandbox_id]["status"])

    async def close(self):
        pass


@pytest.fixture
def client():
    return StubClient()


@pytest.fixture
def be(client):
    return TensorlakeBackend(client=client, sandbox_timeout=777)


def run(coro):
    return asyncio.run(coro)


def test_is_a_sandbox_backend(be):
    assert isinstance(be, SandboxBackend)


def test_create_exec_fork_isolation(be, client):
    async def go():
        root = await be.create()
        (_, image, snap, timeout, name) = client.calls[0]
        assert (snap, timeout) == (None, 777) and name.startswith("sfork-")
        await be.exec(root, "write /a parent")
        child = await be.fork(root)
        await be.exec(child, "write /a child")
        assert (await be.exec(root, "read /a")).output == "parent"
        assert (await be.exec(child, "read /a")).output == "child"
        bad = await be.exec(root, "nope")
        assert (bad.exit_code, bad.output) == (3, "boom")
        assert await be.state(root) == "started"
        await be.delete(root)
        assert await be.state(root) == "destroyed"
        assert await be.state(child) == "started"

    run(go())


def test_fork_many_is_one_copy_call(be, client):
    async def go():
        root = await be.create()
        kids = await be.fork_many(root, 5)
        assert len(set(kids)) == 5
        assert [c for c in client.calls if c[0] == "copy"] == [("copy", root, 5)]

    run(go())


def test_suspended_checkpoint_is_copied_without_waking(be, client):
    async def go():
        root = await be.create()
        await be.exec(root, "write /a v1")
        await be.pause(root)
        assert await be.state(root) == "paused"
        child = await be.fork(root)
        assert await be.state(root) == "paused"
        assert (await be.exec(child, "read /a")).output == "v1"
        await be.resume(root)
        assert (await be.exec(root, "read /a")).output == "v1"  # stale handle was dropped on pause

    run(go())


def test_second_fork_of_a_parent_falls_back_to_snapshot(be, client):
    async def go():
        root = await be.create()
        await be.exec(root, "write /a v1")
        first = await be.fork(root)  # copy -> "<name>-copy"
        second = await be.fork(root)  # name taken -> snapshot fork with a fresh name
        batch = await be.fork_many(root, 3)  # "-copy-1..3" are free -> copy again
        batch2 = await be.fork_many(root, 2)  # "-copy-1..2" taken -> snapshot fork
        kids = [first, second, *batch, *batch2]
        assert len(set(kids)) == 7
        for k in kids:
            assert (await be.exec(k, "read /a")).output == "v1"
            await be.pause(k)  # every child is named, so every child can be suspended
            assert await be.state(k) == "paused"
        assert [c[0] for c in client.calls if c[0] in ("copy", "snapshot")] == [
            "copy",
            "copy",
            "snapshot",
            "copy",
            "copy",
            "snapshot",
        ]
        assert not [k for k in client.snapshots if not k.startswith(("copy-", "suspend-"))]

    run(go())


def test_snapshot_fork_of_a_suspended_parent_re_suspends_it(be, client):
    async def go():
        root = await be.create()
        await be.exec(root, "write /a v1")
        await be.pause(root)
        await be.fork(root)
        child = await be.fork(root)
        assert (await be.exec(child, "read /a")).output == "v1"
        assert await be.state(root) == "paused"

    run(go())


def test_pause_of_an_unnamed_sandbox_is_a_backend_error(be, client):
    async def go():
        root = await be.create()
        client.unnamed_copies = True
        child = await be.fork(root)
        with pytest.raises(BackendError, match="cannot be suspended"):
            await be.pause(child)

    run(go())


def test_partial_copy_failure_cleans_up(be, client):
    async def go():
        root = await be.create()
        client.fail_copy_index = 1
        with pytest.raises(BackendError, match="no_capacity"):
            await be.fork_many(root, 3)
        assert set(client.boxes) == {root}

    run(go())


def test_copy_error_is_translated(be, client):
    async def go():
        root = await be.create()
        await be.delete(root)
        client.boxes[root] = {"status": "terminated", "files": {}, "name": "x"}
        with pytest.raises(BackendError, match="cannot copy"):
            await be.fork(root)

    run(go())


def test_hot_snapshot_is_a_memory_snapshot(be, client):
    async def go():
        root = await be.create()
        await be.exec(root, "write /db v1")
        snap = await be.hot_snapshot(root, "cp")
        assert client.calls[-1] == ("snapshot", root, "memory")
        restored = await be.create(snapshot=snap)
        assert (await be.exec(restored, "read /db")).output == "v1"
        await be.delete_snapshot(snap)
        assert not client.snapshots

    run(go())


def test_engine_batches_forks_through_one_copy(be, client, tmp_path):
    async def go():
        engine = TreeEngine(TreeStore(tmp_path / "t.db"), be)
        _, root = await engine.start_run("task")
        kids = await engine.fork(root.id, ["a", "b", "c"])
        assert len(kids) == 3
        assert [c for c in client.calls if c[0] == "copy"] == [("copy", root.sandbox_id, 3)]
        # The engine pauses the parent after forking it; Tensorlake suspends it.
        assert await be.state(root.sandbox_id) == "paused"

    run(go())


def test_make_backend_knows_new_providers(monkeypatch):
    import sfork.daytona.morph as morph
    import sfork.daytona.tensorlake as tl

    monkeypatch.setattr(morph.MorphBackend, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(tl.TensorlakeBackend, "__init__", lambda self, **kw: None)
    assert type(make_backend("morph")).__name__ == "MorphBackend"
    assert type(make_backend("Tensorlake")).__name__ == "TensorlakeBackend"


def test_delete_cleans_up_server_made_snapshots(be, client):
    async def go():
        root = await be.create()
        await be.pause(root)
        kid = await be.fork(root)  # copy of a suspended root pins root's suspend snapshot
        grandkid = await be.fork(kid)
        await be.pause(grandkid)
        keep = await be.hot_snapshot(kid, "mine")
        await be.delete(root)  # its suspend snapshot is still pinned by kid
        assert any(k.startswith(f"suspend-{root}") for k in client.snapshots)
        await be.delete(grandkid)
        await be.delete(kid)  # unpins root's suspend snapshot, which is retried here
        assert set(client.snapshots) == {keep}

    run(go())
