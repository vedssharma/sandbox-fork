"""E2BBackend against a stub of the e2b AsyncSandbox class (no network)."""

import asyncio
import itertools
from types import SimpleNamespace

import pytest

pytest.importorskip("e2b")

from e2b import CommandExitException, NotFoundException  # noqa: E402

from sfork.core.store import TreeStore  # noqa: E402
from sfork.daytona import BackendError, SandboxBackend, make_backend  # noqa: E402
from sfork.daytona.e2b import E2BBackend  # noqa: E402
from sfork.orchestrator.engine import TreeEngine  # noqa: E402


class StubSandbox:
    """Mimics the class-level API of e2b.AsyncSandbox that E2BBackend uses."""

    ids = itertools.count(1)
    boxes: dict[str, dict] = {}
    snapshots: dict[str, dict] = {}
    calls: list[tuple] = []
    fail_fork_index: int | None = None

    def __init__(self, sid: str):
        self.sandbox_id = sid
        self.commands = SimpleNamespace(run=self._run)

    @classmethod
    def reset(cls):
        cls.ids = itertools.count(1)
        cls.boxes, cls.snapshots, cls.calls, cls.fail_fork_index = {}, {}, [], None

    @classmethod
    def _new(cls, files: dict, parent: str | None = None) -> "StubSandbox":
        sid = f"sb{next(cls.ids)}"
        cls.boxes[sid] = {"state": "running", "files": dict(files), "parent": parent}
        return cls(sid)

    @classmethod
    async def create(cls, template, timeout=None, metadata=None):
        cls.calls.append(("create", template, timeout))
        src = cls.snapshots.get(template, {"files": {}})
        return cls._new(src["files"])

    @classmethod
    async def connect(cls, sandbox_id, timeout=None):
        cls.calls.append(("connect", sandbox_id))
        cls.boxes[sandbox_id]["state"] = "running"
        return cls(sandbox_id)

    @classmethod
    async def fork(cls, sandbox_id, timeout=None, count=None):
        cls.calls.append(("fork", sandbox_id, count))
        box = cls.boxes[sandbox_id]
        if box["state"] != "running":
            raise RuntimeError("can only fork a running sandbox")
        out = []
        for i in range(count):
            if cls.fail_fork_index == i:
                out.append(RuntimeError("no capacity"))
            else:
                out.append(cls._new(box["files"], parent=sandbox_id))
        return out

    @classmethod
    async def pause(cls, sandbox_id, mode=None):
        cls.calls.append(("pause", sandbox_id, mode))
        cls.boxes[sandbox_id]["state"] = "paused"
        return True

    @classmethod
    async def create_snapshot(cls, sandbox_id, name=None, mode=None):
        sid = f"{name}:latest"
        cls.snapshots[sid] = {"files": dict(cls.boxes[sandbox_id]["files"])}
        return SimpleNamespace(snapshot_id=sid, names=[sid])

    @classmethod
    async def delete_snapshot(cls, snapshot_id):
        return cls.snapshots.pop(snapshot_id, None) is not None

    @classmethod
    async def kill(cls, sandbox_id):
        return cls.boxes.pop(sandbox_id, None) is not None

    @classmethod
    async def get_info(cls, sandbox_id):
        if sandbox_id not in cls.boxes:
            raise NotFoundException("gone")
        return SimpleNamespace(state=cls.boxes[sandbox_id]["state"])

    async def _run(self, cmd, user=None, timeout=None):
        files = self.boxes[self.sandbox_id]["files"]
        if cmd.startswith("write "):
            _, path, val = cmd.split(" ", 2)
            files[path] = val
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        if cmd.startswith("read "):
            return SimpleNamespace(exit_code=0, stdout=files.get(cmd[5:], ""), stderr="")
        if "sfork-guest post-fork" in cmd:
            return SimpleNamespace(exit_code=0, stdout="ready\n", stderr="")
        raise CommandExitException(stderr="boom", stdout="", exit_code=3, error="boom")


@pytest.fixture
def be():
    StubSandbox.reset()
    return E2BBackend(sandbox_cls=StubSandbox, sandbox_timeout=1234)


def run(coro):
    return asyncio.run(coro)


def test_is_a_sandbox_backend(be):
    assert isinstance(be, SandboxBackend)


def test_create_exec_fork_isolation(be):
    async def go():
        root = await be.create()
        assert StubSandbox.calls[0] == ("create", "base", 1234)
        await be.exec(root, "write /a parent")
        child = await be.fork(root)
        await be.exec(child, "write /a child")
        assert (await be.exec(root, "read /a")).output == "parent"
        assert (await be.exec(child, "read /a")).output == "child"
        bad = await be.exec(root, "nope")
        assert (bad.exit_code, bad.output) == (3, "boom")
        assert await be.state(root) == "started"
        await be.delete(root)  # E2B children don't pin their parent
        assert await be.state(root) == "destroyed"

    run(go())


def test_fork_many_uses_one_checkpoint_and_batches_by_20(be):
    async def go():
        root = await be.create()
        kids = await be.fork_many(root, 23)
        assert len(set(kids)) == 23
        assert [c for c in StubSandbox.calls if c[0] == "fork"] == [("fork", root, 20), ("fork", root, 3)]

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


def test_partial_fork_failure_cleans_up(be):
    async def go():
        root = await be.create()
        StubSandbox.fail_fork_index = 1
        with pytest.raises(BackendError, match="no capacity"):
            await be.fork_many(root, 3)
        assert set(StubSandbox.boxes) == {root}

    run(go())


def test_hot_snapshot_roundtrip(be):
    async def go():
        root = await be.create()
        await be.exec(root, "write /db v1")
        snap = await be.hot_snapshot(root, "cp")
        restored = await be.create(snapshot=snap)
        assert (await be.exec(restored, "read /db")).output == "v1"
        await be.delete_snapshot(snap)
        assert not StubSandbox.snapshots

    run(go())


def test_engine_batches_forks_through_fork_many(be, tmp_path):
    async def go():
        engine = TreeEngine(TreeStore(tmp_path / "t.db"), be)
        run_, root = await engine.start_run("task")
        kids = await engine.fork(root.id, ["a", "b", "c"])
        assert len(kids) == 3
        assert [c for c in StubSandbox.calls if c[0] == "fork"] == [("fork", root.sandbox_id, 3)]

    run(go())


def test_make_backend_rejects_unknown():
    with pytest.raises(ValueError):
        make_backend("nope")
