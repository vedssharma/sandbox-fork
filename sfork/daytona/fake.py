"""In-process fake of Daytona VM sandboxes.

Models what matters to the tree engine: each sandbox has a file system, a state, a parent, and
"memory" (a dict standing in for running process state). Forks copy files and memory, and enforce
Daytona's documented rule that a parent can't be deleted while it has live fork children.

`exec` understands a deliberately tiny shell subset (enough for tests and the demo):
    echo TEXT > PATH | echo TEXT >> PATH | cat PATH | rm PATH | ls [DIR] | true | false | exit N
plus any command containing `sfork-guest post-fork` (the guest hook), which regenerates /etc/machine-id.
Anything else returns exit code 127 so tests don't silently pass on unsupported commands.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .base import BackendError, ExecResult, ParentHasChildrenError

Handler = Callable[["FakeSandbox", str], ExecResult | None]


@dataclass
class FakeSandbox:
    id: str
    name: str
    parent_id: str | None
    files: dict[str, str] = field(default_factory=dict)
    memory: dict[str, object] = field(default_factory=dict)
    state: str = "started"
    history: list[str] = field(default_factory=list)


@dataclass
class OpStats:
    calls: dict[str, int] = field(default_factory=dict)

    def bump(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1


class FakeBackend:
    def __init__(self, *, latency: float = 0.0, handlers: list[Handler] | None = None) -> None:
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.snapshots: dict[str, tuple[dict[str, str], dict[str, object]]] = {}
        self.stats = OpStats()
        self._latency = latency
        self._handlers = list(handlers or [])
        self._counter = itertools.count(1)

    # helpers ----------------------------------------------------------------
    async def _tick(self, op: str) -> None:
        self.stats.bump(op)
        if self._latency:
            await asyncio.sleep(self._latency)

    def _get(self, sandbox_id: str) -> FakeSandbox:
        sb = self.sandboxes.get(sandbox_id)
        if sb is None or sb.state == "destroyed":
            raise BackendError(f"sandbox {sandbox_id} not found")
        return sb

    def _new_id(self) -> str:
        return f"fake-{next(self._counter):04d}"

    def live_ids(self) -> set[str]:
        return {i for i, s in self.sandboxes.items() if s.state != "destroyed"}

    def add_handler(self, handler: Handler) -> None:
        self._handlers.append(handler)

    # SandboxBackend ---------------------------------------------------------
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        await self._tick("create")
        sid = self._new_id()
        files: dict[str, str] = {"/etc/machine-id": uuid.uuid4().hex}
        memory: dict[str, object] = {}
        if snapshot is not None:
            if snapshot not in self.snapshots:
                raise BackendError(f"snapshot {snapshot} not found")
            snap_files, snap_mem = self.snapshots[snapshot]
            files, memory = dict(snap_files), dict(snap_mem)
        self.sandboxes[sid] = FakeSandbox(sid, name or sid, None, files, memory)
        return sid

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        await self._tick("fork")
        parent = self._get(sandbox_id)
        sid = self._new_id()
        self.sandboxes[sid] = FakeSandbox(
            sid, name or sid, parent.id, dict(parent.files), dict(parent.memory)
        )
        return sid

    async def pause(self, sandbox_id: str) -> None:
        await self._tick("pause")
        self._get(sandbox_id).state = "paused"

    async def resume(self, sandbox_id: str) -> None:
        await self._tick("resume")
        self._get(sandbox_id).state = "started"

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        await self._tick("hot_snapshot")
        sb = self._get(sandbox_id)
        if sb.state != "started":
            raise BackendError("memory snapshots require a started sandbox")
        self.snapshots[name] = (dict(sb.files), dict(sb.memory))
        return name

    async def delete_snapshot(self, name: str) -> None:
        await self._tick("delete_snapshot")
        self.snapshots.pop(name, None)

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        await self._tick("exec")
        sb = self._get(sandbox_id)
        if sb.state != "started":
            raise BackendError(f"sandbox {sandbox_id} is {sb.state}, not started")
        sb.history.append(command)
        for handler in self._handlers:
            res = handler(sb, command)
            if res is not None:
                return res
        return _mini_shell(sb, command)

    async def delete(self, sandbox_id: str) -> None:
        await self._tick("delete")
        sb = self._get(sandbox_id)
        live_children = [
            s for s in self.sandboxes.values() if s.parent_id == sb.id and s.state != "destroyed"
        ]
        if live_children:
            raise ParentHasChildrenError(
                f"sandbox {sandbox_id} has live forks: {[c.id for c in live_children]}"
            )
        sb.state = "destroyed"

    async def state(self, sandbox_id: str) -> str:
        sb = self.sandboxes.get(sandbox_id)
        return "destroyed" if sb is None else sb.state

    async def close(self) -> None:
        return None


def _mini_shell(sb: FakeSandbox, command: str) -> ExecResult:
    command = command.strip()
    if "sfork-guest post-fork" in command:
        sb.files["/etc/machine-id"] = uuid.uuid4().hex
        return ExecResult(0, "ready\n")
    for op in (">>", ">"):
        if command.startswith("echo ") and f" {op} " in command:
            text, path = command[5:].rsplit(f" {op} ", 1)
            text = " ".join(shlex.split(text))
            path = path.strip()
            prev = sb.files.get(path, "") if op == ">>" else ""
            sb.files[path] = prev + text + "\n"
            return ExecResult(0, "")
    parts = shlex.split(command)
    if not parts:
        return ExecResult(0, "")
    cmd, args = parts[0], parts[1:]
    if cmd == "true":
        return ExecResult(0, "")
    if cmd == "false":
        return ExecResult(1, "")
    if cmd == "exit":
        return ExecResult(int(args[0]) if args else 0, "")
    if cmd == "echo":
        return ExecResult(0, " ".join(args) + "\n")
    if cmd == "cat" and len(args) == 1:
        if args[0] not in sb.files:
            return ExecResult(1, f"cat: {args[0]}: No such file or directory\n")
        return ExecResult(0, sb.files[args[0]])
    if cmd == "rm" and args:
        path = args[-1]
        if path not in sb.files:
            return ExecResult(1, f"rm: cannot remove '{path}'\n")
        del sb.files[path]
        return ExecResult(0, "")
    if cmd == "ls":
        prefix = (args[0].rstrip("/") + "/") if args else "/"
        names = sorted({p[len(prefix) :].split("/")[0] for p in sb.files if p.startswith(prefix)})
        return ExecResult(0, "".join(n + "\n" for n in names))
    return ExecResult(127, f"fake shell: unsupported command: {command}\n")
