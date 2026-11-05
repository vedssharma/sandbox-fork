"""Daytona Linux VM backend.

Reads DAYTONA_API_KEY (and optionally DAYTONA_API_URL / DAYTONA_TARGET) from the environment via
the SDK. Only the Linux VM class supports fork and memory snapshots, so the default snapshot is a VM
one (`daytona-vm-small`); override with SFORK_VM_SNAPSHOT or the `default_snapshot` argument.

Notes on SDK coverage (daytona 0.230):
- `sandbox.fork()` and `sandbox.pause()` exist in the SDK.
- Resume is `sandbox.start()` on a paused VM.
- Hot (memory) snapshots are REST-only, so we call the generated API client the SDK wraps,
  `create_sandbox_snapshot(..., CreateSandboxSnapshot(include_memory=True))`, and poll until the
  snapshot is active.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .base import BackendError, ExecResult, ParentHasChildrenError

T = TypeVar("T")

DEFAULT_VM_SNAPSHOT = os.environ.get("SFORK_VM_SNAPSHOT", "daytona-vm-small")


class DaytonaBackend:
    def __init__(
        self,
        *,
        default_snapshot: str = DEFAULT_VM_SNAPSHOT,
        op_timeout: float = 300,
        max_retries: int = 6,
        labels: dict[str, str] | None = None,
    ) -> None:
        from daytona import AsyncDaytona

        self._client = AsyncDaytona()
        self._default_snapshot = default_snapshot
        self._op_timeout = op_timeout
        self._max_retries = max_retries
        self._labels = {"sfork": "1", **(labels or {})}
        self._cache: dict[str, object] = {}  # sandbox id -> AsyncSandbox

    # helpers ----------------------------------------------------------------
    async def _retry(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Retry on 429 / transient 5xx with jittered exponential backoff."""
        from daytona import DaytonaError, DaytonaRateLimitError

        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                return await fn()
            except DaytonaRateLimitError:
                if attempt == self._max_retries:
                    raise
            except DaytonaError as e:
                code = getattr(e, "status_code", None)
                if code is None or code < 500 or attempt == self._max_retries:
                    raise _translate(op, e) from e
            await asyncio.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    async def _sb(self, sandbox_id: str):
        sb = self._cache.get(sandbox_id)
        if sb is None:
            sb = await self._retry("get", lambda: self._client.get(sandbox_id))
            self._cache[sandbox_id] = sb
        return sb

    # SandboxBackend ---------------------------------------------------------
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        from daytona import CreateSandboxFromSnapshotParams

        params = CreateSandboxFromSnapshotParams(
            name=name,
            snapshot=snapshot or self._default_snapshot,
            labels=self._labels,
            # Daytona's default for pausable classes: auto-pause after 60 idle minutes, so leaked
            # VMs stop burning CPU quota.
        )
        sb = await self._retry("create", lambda: self._client.create(params, timeout=self._op_timeout))
        self._cache[sb.id] = sb
        return sb.id

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        parent = await self._sb(sandbox_id)
        child = await self._retry("fork", lambda: parent.fork(name=name, timeout=self._op_timeout))
        self._cache[child.id] = child
        return child.id

    async def pause(self, sandbox_id: str) -> None:
        sb = await self._sb(sandbox_id)
        await self._retry("pause", lambda: sb.pause(timeout=self._op_timeout))

    async def resume(self, sandbox_id: str) -> None:
        sb = await self._sb(sandbox_id)
        await self._retry("resume", lambda: sb.start(timeout=self._op_timeout))

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        from daytona_api_client_async.models.create_sandbox_snapshot import CreateSandboxSnapshot

        sb = await self._sb(sandbox_id)
        api = sb._sandbox_api  # noqa: SLF001 - memory snapshots are not wrapped by the SDK yet
        req = CreateSandboxSnapshot(name=name, include_memory=True)
        await self._retry("hot_snapshot", lambda: api.create_sandbox_snapshot(sandbox_id, req))
        deadline = time.monotonic() + self._op_timeout
        while True:
            snap = await self._retry("snapshot_get", lambda: self._client.snapshot.get(name))
            state = str(getattr(snap.state, "value", snap.state))
            if state == "active":
                return name
            if state in ("error", "build_failed"):
                raise BackendError(f"hot snapshot {name} failed: {getattr(snap, 'error_reason', state)}")
            if time.monotonic() > deadline:
                raise BackendError(f"hot snapshot {name} not active after {self._op_timeout}s ({state})")
            await asyncio.sleep(0.5)

    async def delete_snapshot(self, name: str) -> None:
        snap = await self._retry("snapshot_get", lambda: self._client.snapshot.get(name))
        await self._retry("snapshot_delete", lambda: self._client.snapshot.delete(snap))

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        sb = await self._sb(sandbox_id)
        res = await self._retry("exec", lambda: sb.process.exec(command, timeout=timeout))
        return ExecResult(exit_code=res.exit_code, output=res.result)

    async def delete(self, sandbox_id: str) -> None:
        sb = await self._sb(sandbox_id)
        await self._retry("delete", lambda: sb.delete(timeout=self._op_timeout))
        self._cache.pop(sandbox_id, None)

    async def state(self, sandbox_id: str) -> str:
        from daytona import DaytonaNotFoundError

        try:
            sb = await self._sb(sandbox_id)
            await sb.refresh_data()
        except DaytonaNotFoundError:
            return "destroyed"
        return str(getattr(sb.state, "value", sb.state))

    async def close(self) -> None:
        await self._client.close()


def _translate(op: str, e: Exception) -> Exception:
    msg = str(e).lower()
    if op == "delete" and ("fork" in msg or "child" in msg):
        return ParentHasChildrenError(str(e))
    return BackendError(f"{op} failed: {e}")
