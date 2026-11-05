"""Local-directory backend for tests and offline dry runs. NOT a sandbox: commands run on this host.

Each "sandbox" is a temporary directory; `fork` copies it, so file state forks but processes and
memory don't. Commands run with real bash, and every occurrence of `alias` (default `/workspace`) in
a command is rewritten to the sandbox's directory, so code written for `/workspace` on E2B works
unchanged. Use it to exercise the agent loop and evaluator end to end without credentials.
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
import tempfile
from pathlib import Path

from .base import BackendError, ExecResult


class LocalBackend:
    def __init__(self, *, alias: str = "/workspace", root: str | Path | None = None) -> None:
        self.alias = alias.rstrip("/")
        self._root = Path(root) if root else Path(tempfile.mkdtemp(prefix="sfork-local-"))
        self._dirs: dict[str, Path] = {}
        self._state: dict[str, str] = {}
        self._snapshots: dict[str, Path] = {}
        self._ids = itertools.count(1)

    def _dir(self, sandbox_id: str) -> Path:
        if self._state.get(sandbox_id) in (None, "destroyed"):
            raise BackendError(f"sandbox {sandbox_id} not found")
        return self._dirs[sandbox_id]

    def _new(self) -> tuple[str, Path]:
        sid = f"local-{next(self._ids):04d}"
        return sid, self._root / sid

    def live_ids(self) -> set[str]:
        return {s for s, st in self._state.items() if st != "destroyed"}

    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        sid, d = self._new()
        if snapshot is not None:
            if snapshot not in self._snapshots:
                raise BackendError(f"snapshot {snapshot} not found")
            shutil.copytree(self._snapshots[snapshot], d, symlinks=True)
        else:
            d.mkdir(parents=True)
        self._dirs[sid], self._state[sid] = d, "started"
        return sid

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        src = self._dir(sandbox_id)
        sid, d = self._new()
        shutil.copytree(src, d, symlinks=True)
        self._dirs[sid], self._state[sid] = d, "started"
        return sid

    async def pause(self, sandbox_id: str) -> None:
        self._dir(sandbox_id)
        self._state[sandbox_id] = "paused"

    async def resume(self, sandbox_id: str) -> None:
        self._dir(sandbox_id)
        self._state[sandbox_id] = "started"

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        dst = self._root / f"snap-{name}"
        shutil.copytree(self._dir(sandbox_id), dst, symlinks=True)
        self._snapshots[name] = dst
        return name

    async def delete_snapshot(self, name: str) -> None:
        p = self._snapshots.pop(name, None)
        if p:
            shutil.rmtree(p, ignore_errors=True)

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        d = self._dir(sandbox_id)
        if self._state[sandbox_id] != "started":
            raise BackendError(f"sandbox {sandbox_id} is {self._state[sandbox_id]}, not started")
        command = command.replace(self.alias, str(d))
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=d,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout or None)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(124, f"timed out after {timeout}s\n")
        return ExecResult(proc.returncode or 0, out.decode(errors="replace").replace(str(d), self.alias))

    async def delete(self, sandbox_id: str) -> None:
        d = self._dir(sandbox_id)
        shutil.rmtree(d, ignore_errors=True)
        self._state[sandbox_id] = "destroyed"

    async def state(self, sandbox_id: str) -> str:
        return self._state.get(sandbox_id, "destroyed")

    async def close(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)
