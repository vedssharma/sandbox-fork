"""`sfork` CLI: drive the execution tree by hand (Phase 1).

State lives in a SQLite file (SFORK_DB, default ~/.sfork/tree.db). Commands that touch sandboxes use
a real backend chosen by SFORK_BACKEND (`e2b`, the default, needs E2B_API_KEY;
`daytona` needs DAYTONA_API_KEY). `sfork demo` runs entirely on the in-process fake.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer

from .core.budget import Budget
from .core.store import TreeStore
from .daytona.fake import FakeBackend
from .orchestrator.engine import TreeEngine

app = typer.Typer(help="Fork-on-action execution trees over E2B or Daytona sandboxes.", no_args_is_help=True)


def _db_path() -> Path:
    p = Path(os.environ.get("SFORK_DB", Path.home() / ".sfork" / "tree.db"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _with_engine[T](fn: Callable[[TreeEngine], Awaitable[T]]) -> T:
    async def go() -> T:
        from .daytona import make_backend

        backend = make_backend()
        store = TreeStore(_db_path())
        try:
            return await fn(TreeEngine(store, backend, budget=Budget()))
        finally:
            await backend.close()
            store.close()

    return asyncio.run(go())


@app.command()
def run(task: str, snapshot: Annotated[str | None, typer.Option(help="VM snapshot")] = None) -> None:
    """Start a run: create the root VM sandbox."""

    async def go(e: TreeEngine):
        r, root = await e.start_run(task, snapshot=snapshot)
        typer.echo(f"run {r.id}\nroot {root.id} (sandbox {root.sandbox_id})")

    _with_engine(go)


@app.command()
def fork(node: str, hypotheses: list[str]) -> None:
    """Fork NODE into one child per hypothesis. NODE becomes a paused restore point."""

    async def go(e: TreeEngine):
        for c in await e.fork(node, hypotheses):
            typer.echo(f"{c.id}  {c.hypothesis!r}  sandbox={c.sandbox_id}  fork={c.meta['fork_seconds']}s")

    _with_engine(go)


@app.command("exec")
def exec_(node: str, command: str, timeout: int = 600) -> None:
    """Run a shell command in a running node."""

    async def go(e: TreeEngine):
        res = await e.exec(node, command, timeout=timeout)
        typer.echo(res.output, nl=False)
        raise typer.Exit(res.exit_code)

    _with_engine(go)


@app.command()
def checkpoint(node: str, label: str) -> None:
    """Freeze NODE as a labeled restore point; prints the new working node to continue in."""

    async def go(e: TreeEngine):
        cp, work = await e.checkpoint(node, label)
        typer.echo(f"checkpoint {cp.id}\nworking {work.id}")

    _with_engine(go)


@app.command()
def rewind(
    checkpoint_id: str,
    discard: Annotated[str | None, typer.Option(help="node to abandon and delete")] = None,
    lesson: Annotated[str | None, typer.Option(help="what went wrong, carried forward")] = None,
) -> None:
    """Start a fresh node from a checkpoint (the checkpoint itself is left untouched)."""

    async def go(e: TreeEngine):
        n = await e.rewind(checkpoint_id, discard=discard, lesson=lesson)
        typer.echo(f"{n.id}  sandbox={n.sandbox_id}")

    _with_engine(go)


@app.command()
def promote(node: str) -> None:
    """Make NODE the head and delete its competing siblings."""

    async def go(e: TreeEngine):
        deleted = await e.promote(node)
        typer.echo(f"head={node}; deleted {len(deleted)} node(s): {' '.join(deleted)}")

    _with_engine(go)


@app.command()
def gc(run_id: str) -> None:
    """Delete lost nodes, leaves first."""

    async def go(e: TreeEngine):
        typer.echo(" ".join(await e.gc(run_id)) or "nothing to delete")

    _with_engine(go)


@app.command()
def compact(run_id: str) -> None:
    """Hot-snapshot the head into a fresh root and delete the old chain."""

    async def go(e: TreeEngine):
        n = await e.compact(run_id)
        typer.echo(f"new root {n.id} sandbox={n.sandbox_id}")

    _with_engine(go)


@app.command()
def finish(run_id: str, keep_head: bool = False) -> None:
    """End a run and delete its sandboxes."""

    async def go(e: TreeEngine):
        typer.echo(f"deleted {len(await e.finish_run(run_id, keep_head=keep_head))} sandbox(es)")

    _with_engine(go)


@app.command()
def tree(run_id: str) -> None:
    """Print a run's execution tree (no sandbox calls)."""
    store = TreeStore(_db_path())
    typer.echo(TreeEngine(store, FakeBackend()).render_tree(run_id))


@app.command()
def runs() -> None:
    """List runs (no sandbox calls)."""
    store = TreeStore(_db_path())
    for r in store.list_runs():
        typer.echo(f"{r.id}  {r.status}  head={r.head_node_id}  {r.task}")


@app.command()
def demo() -> None:
    """Walk the schema-migration scenario on the in-process fake: fork, break one branch, rewind."""

    async def go():
        e = TreeEngine(TreeStore(), FakeBackend())
        r, root = await e.start_run("add an email column without losing data")
        await e.exec(root.id, "echo users(id,name) rows=3 > /db/schema")
        typer.echo("$ fork root into two migration strategies")
        a, b = await e.fork(root.id, ["additive: ALTER TABLE ADD COLUMN", "rebuild: DROP + CREATE"])
        await e.exec(a.id, "echo users(id,name,email) rows=3 > /db/schema")
        await e.exec(b.id, "echo users(id,name,email) rows=0 > /db/schema")
        for n in (a, b):
            typer.echo(f"  {n.hypothesis}: {(await e.exec(n.id, 'cat /db/schema')).output.strip()}")
        typer.echo("$ promote the branch that kept the rows")
        await e.promote(a.id)
        typer.echo("$ checkpoint, then try a risky follow-up and rewind it")
        cp, work = await e.checkpoint(a.id, "before backfill")
        await e.exec(work.id, "rm /db/schema")
        fresh = await e.rewind(cp.id, discard=work.id, lesson="backfill script dropped the table")
        typer.echo(f"  after rewind: {(await e.exec(fresh.id, 'cat /db/schema')).output.strip()}")
        typer.echo("")
        typer.echo(e.render_tree(r.id))

    asyncio.run(go())


@app.command()
def explore(
    task_dir: Annotated[Path, typer.Argument(help="task directory: task.md, sandbox-fork.toml, repo/")],
    backend: Annotated[
        str | None, typer.Option(help="e2b (default) | daytona | local (no isolation!)")
    ] = None,
    model: Annotated[str, typer.Option(help="model for the root agent")] = "claude-opus-5-5",
    worker_model: Annotated[
        str | None, typer.Option(help="model for forked branches (default: --model)")
    ] = None,
    effort: Annotated[str, typer.Option(help="low | medium | high | xhigh | max")] = "medium",
    max_steps: Annotated[int, typer.Option(help="LLM turns per branch")] = 30,
    max_fanout: int = 3,
    max_fork_depth: Annotated[int, typer.Option(help="1 = branches can't fork again")] = 1,
    orchestrator_fork: Annotated[
        bool, typer.Option(help="fork the root into the task's [[alternatives]] before the agent starts")
    ] = False,
    keep_head: Annotated[bool, typer.Option(help="leave the winning sandbox running")] = False,
    report: Annotated[Path | None, typer.Option(help="write the JSON report here")] = None,
) -> None:
    """Run an agent on a task; at fork points it branches the sandbox, and the best branch wins."""
    import json
    import time

    from .agent.llm import AnthropicLLM
    from .daytona import make_backend
    from .orchestrator.explore import ExploreConfig, Explorer, Task

    task = Task.load(task_dir)
    name = (backend or os.environ.get("SFORK_BACKEND") or "e2b").lower()
    if name == "local":
        typer.secho("local backend: commands run on THIS machine, not in a sandbox", fg="yellow", err=True)
    cfg = ExploreConfig(
        model=model,
        worker_model=worker_model,
        max_steps_root=max_steps,
        max_steps_branch=max_steps,
        max_fanout=max_fanout,
        max_fork_depth=max_fork_depth,
        keep_head=keep_head,
        initial_alternatives=Task.alternatives_from(task_dir) if orchestrator_fork else None,
    )

    def show(e: dict) -> None:
        kind = e["type"]
        who = e.get("branch") or "root"
        if kind == "tool":
            typer.echo(f"  [{who}] {e['name']} {e.get('arg', '')}"[:160])
        elif kind == "say":
            typer.echo(f"  [{who}] says: {e['text']}"[:200])
        elif kind != "seeded":
            typer.secho(
                f"* {kind}: " + json.dumps({k: v for k, v in e.items() if k not in ("t", "type")}), fg="cyan"
            )

    async def go() -> dict:
        be = make_backend(name)
        llm = AnthropicLLM(effort=effort)
        engine = TreeEngine(
            TreeStore(_db_path()),
            be,
            budget=Budget(max_fanout=max(max_fanout, 1)),
            run_post_fork_hook=name != "local",
        )
        try:
            return await Explorer(engine, llm, cfg, on_event=show).run(task)
        finally:
            await llm.close()
            await be.close()

    rep = asyncio.run(go())
    out = (
        report
        or Path("bench/results")
        / f"explore-{task.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, default=str))
    typer.echo("")
    typer.echo(rep["tree"])
    for fp in rep["fork_points"]:
        typer.echo(f"\nfork at {fp['node']} ({fp['fork_seconds']}s): {fp['why']}")
    typer.echo(
        f"\nhead: {rep.get('head_branch') or 'root'}  final: {rep.get('final_evaluation', {}).get('passed')}"
        f"/{rep.get('final_evaluation', {}).get('total')} tests"
    )
    typer.echo(f"usage: {json.dumps(rep['usage_by_model'])}")
    typer.echo(
        f"sandboxes deleted: {rep['sandboxes_deleted']}, leftover: {rep['leftover_sandboxes'] or 'none'}"
    )
    typer.echo(f"wall: {rep['wall_seconds']}s  report: {out}")


if __name__ == "__main__":
    app()
