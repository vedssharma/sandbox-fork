import asyncio

from sfork.core.models import Node, NodeKind, Run
from sfork.core.store import TreeStore
from sfork.daytona import FakeBackend, SandboxBackend
from sfork.guest import MARKER, post_fork_command, post_fork_script


def test_store_roundtrip_and_ancestors(tmp_path):
    store = TreeStore(tmp_path / "t.db")
    run = Run(task="x")
    store.put_run(run)
    a = Node(run_id=run.id, sandbox_id="s1", kind=NodeKind.ROOT)
    b = Node(run_id=run.id, sandbox_id="s2", kind=NodeKind.BRANCH, parent_id=a.id, depth=1, meta={"k": 1})
    c = Node(run_id=run.id, sandbox_id="s3", kind=NodeKind.BRANCH, parent_id=b.id, depth=2)
    for n in (a, b, c):
        store.put_node(n)
    store.close()

    store = TreeStore(tmp_path / "t.db")
    assert store.get_node(b.id).meta == {"k": 1}
    assert [n.id for n in store.ancestors(c.id)] == [b.id, a.id]
    assert [n.id for n in store.children(a.id)] == [b.id]
    assert store.get_run(run.id).task == "x"


def test_fake_backend_is_a_sandbox_backend():
    assert isinstance(FakeBackend(), SandboxBackend)


def test_fake_shell_and_snapshot():
    async def go():
        be = FakeBackend()
        s = await be.create()
        assert (await be.exec(s, "echo a b > /x")).ok
        assert (await be.exec(s, "echo c >> /x")).ok
        assert (await be.exec(s, "cat /x")).output == "a b\nc\n"
        assert (await be.exec(s, "ls /")).output == "etc\nx\n"
        assert (await be.exec(s, "python -V")).exit_code == 127
        await be.hot_snapshot(s, "snap")
        t = await be.create(snapshot="snap")
        assert (await be.exec(t, "cat /x")).output == "a b\nc\n"

    asyncio.run(go())


def test_guest_script_is_packaged():
    assert MARKER in post_fork_script()
    cmd = post_fork_command(hostname="node-1")
    assert cmd.startswith("SFORK_HOSTNAME=node-1 bash -c ")


def test_cli_demo_runs():
    from typer.testing import CliRunner

    from sfork.cli import app

    res = CliRunner().invoke(app, ["demo"])
    assert res.exit_code == 0, res.output
    assert "after rewind: users(id,name,email) rows=3" in res.output
