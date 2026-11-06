"""Tensorlake backend.

Reads TENSORLAKE_API_KEY from the environment via the SDK. The network policy must allow
`api.tensorlake.ai` (control plane) and `*.sandbox.tensorlake.ai` (exec goes to
`<sandbox-id>.sandbox.tensorlake.ai`; allowing only the bare host makes every exec fail).

How our protocol maps onto Tensorlake (SDK `tensorlake` 0.5.162):
- `fork` / `fork_many` -> `AsyncSandboxClient.copy(sandbox_id, times=N)`, a live copy that carries
  filesystem, memory and running processes into N new running sandboxes. Unlike E2B and Morph,
  Tensorlake can copy a *suspended* sandbox from its retained checkpoint, so forking a paused
  checkpoint doesn't wake it. Partial failures come back per copy; we delete the survivors and raise.
- `pause` / `resume` -> `suspend()` / `resume()`. Only named sandboxes can be suspended, so every
  sandbox we create gets a unique `sfork-...` name, and an unnamed copy is named before suspending.
- `hot_snapshot` -> `snapshot_and_wait(snapshot_type=MEMORY)`; returns the snapshot id, which
  `create` accepts in place of an image.
- Copies are independent sandboxes, so `delete` never raises `ParentHasChildrenError`.
- The free plan allows one concurrent sandbox, which is not enough to fork; a credit pack is needed.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .base import BackendError, ExecResult

T = TypeVar("T")

DEFAULT_IMAGE = os.environ.get("SFORK_TENSORLAKE_IMAGE") or None  # None = Tensorlake's default image
DEFAULT_CPUS = float(os.environ.get("SFORK_TENSORLAKE_CPUS", "1"))
DEFAULT_MEMORY = int(os.environ.get("SFORK_TENSORLAKE_MEMORY", "1024"))  # MiB
DEFAULT_TIMEOUT = int(os.environ.get("SFORK_TENSORLAKE_TIMEOUT", "3600"))

# Tensorlake statuses in Daytona's vocabulary, so callers see the same states from every backend.
_STATES = {
    "running": "started",
    "suspended": "paused",
    "terminated": "destroyed",
    "timeout": "destroyed",
    "failed": "error",
}


def _new_name() -> str:
    return f"sfork-{uuid.uuid4().hex[:12]}"


class TensorlakeBackend:
    def __init__(
        self,
        *,
        image: str | None = DEFAULT_IMAGE,
        cpus: float = DEFAULT_CPUS,
        memory_mb: int = DEFAULT_MEMORY,
        sandbox_timeout: int = DEFAULT_TIMEOUT,
        exec_user: str | None = None,
        max_retries: int = 6,
        client: Any = None,
    ) -> None:
        if client is None:
            from tensorlake.sandbox import AsyncSandboxClient

            client = AsyncSandboxClient.for_cloud()
        self._client = client
        self._image, self._cpus, self._memory = image, cpus, memory_mb
        self._timeout = sandbox_timeout
        self._user = exec_user
        self._max_retries = max_retries
        self._conns: dict[str, Any] = {}  # sandbox id -> AsyncSandbox (exec handle)
        self._named: set[str] = set()
        self._fork_locks: dict[str, asyncio.Lock] = {}

    # helpers ----------------------------------------------------------------
    async def _retry(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Retry on 429 / 5xx with jittered exponential backoff; translate other errors."""
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                return await fn()
            except BackendError:
                raise
            except Exception as e:  # noqa: BLE001 - SandboxError, RemoteAPIError and transport errors
                status = getattr(e, "status_code", None)
                retryable = status == 429 or (isinstance(status, int) and status >= 500 and status != 504)
                if not retryable or attempt == self._max_retries:
                    raise BackendError(f"{op} failed: {e}") from e
            await asyncio.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    async def _conn(self, sandbox_id: str) -> Any:
        sb = self._conns.get(sandbox_id)
        if sb is None:
            sb = await self._retry("connect", lambda: self._client.connect(sandbox_id))
            self._conns[sandbox_id] = sb
        return sb

    async def _drop_conn(self, sandbox_id: str) -> None:
        sb = self._conns.pop(sandbox_id, None)
        close = getattr(sb, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 - a stale handle is fine to drop
                pass

    # SandboxBackend ---------------------------------------------------------
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        sb_name = _new_name()
        kwargs: dict[str, Any] = {"timeout_secs": self._timeout, "name": sb_name}
        if snapshot:
            kwargs["snapshot_id"] = snapshot
        else:
            kwargs.update(image=self._image, cpus=self._cpus, memory_mb=self._memory)
        res = await self._retry("create", lambda: self._client.create(**kwargs))
        status = str(getattr(res.status, "value", res.status))
        if status != "running":
            # A blocking create can answer while still pending; connect() waits for readiness.
            await self._conn(res.sandbox_id)
        self._named.add(res.sandbox_id)
        return res.sandbox_id

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        return (await self.fork_many(sandbox_id, 1))[0]

    async def fork_many(self, sandbox_id: str, count: int) -> list[str]:
        """Live-copy `sandbox_id` into `count` running sandboxes. All-or-nothing."""
        if count < 1:
            return []
        lock = self._fork_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            try:
                res = await self._client.copy(sandbox_id, times=count)
            except Exception as e:  # noqa: BLE001
                # 422 / 504 can still have created some copies; the SDK raises without them, so
                # the safest thing is to report the failure. Retry only throttling.
                if getattr(e, "status_code", None) == 429:
                    res = await self._retry("fork", lambda: self._client.copy(sandbox_id, times=count))
                else:
                    raise BackendError(f"fork failed: {e}") from e
        made = [c for c in res.sandboxes if c.status == "running"]
        failed = [c for c in res.sandboxes if c.status != "running"]
        for c in made:
            if c.name:
                self._named.add(c.sandbox_id)
        if failed or len(made) != count:
            for c in made:
                try:
                    await self.delete(c.sandbox_id)
                except BackendError:
                    pass
            why = failed[0].reason if failed else f"asked for {count}, got {len(made)}"
            raise BackendError(f"fork failed: {why}")
        return [c.sandbox_id for c in made]

    async def pause(self, sandbox_id: str) -> None:
        if sandbox_id not in self._named:
            await self._retry("name", lambda: self._client.update_sandbox(sandbox_id, name=_new_name()))
            self._named.add(sandbox_id)
        await self._drop_conn(sandbox_id)
        await self._retry("pause", lambda: self._client.suspend(sandbox_id))

    async def resume(self, sandbox_id: str) -> None:
        await self._retry("resume", lambda: self._client.resume(sandbox_id))

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        from tensorlake.sandbox import SnapshotType

        info = await self._retry(
            "hot_snapshot",
            lambda: self._client.snapshot_and_wait(sandbox_id, snapshot_type=SnapshotType.MEMORY),
        )
        return info.snapshot_id

    async def delete_snapshot(self, name: str) -> None:
        await self._retry("snapshot_delete", lambda: self._client.delete_snapshot(name))

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        sb = await self._conn(sandbox_id)
        kwargs: dict[str, Any] = {"args": ["-c", command]}
        if timeout:
            kwargs["timeout"] = float(timeout)
        if self._user:
            kwargs["user"] = self._user
        res = await self._retry("exec", lambda: sb.run("sh", **kwargs))
        return ExecResult(exit_code=res.exit_code, output=(res.stdout or "") + (res.stderr or ""))

    async def delete(self, sandbox_id: str) -> None:
        await self._drop_conn(sandbox_id)
        await self._retry("delete", lambda: self._client.delete(sandbox_id))
        self._named.discard(sandbox_id)
        self._fork_locks.pop(sandbox_id, None)

    async def state(self, sandbox_id: str) -> str:
        try:
            info = await self._client.get(sandbox_id)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "SandboxNotFoundError" or getattr(e, "status_code", None) == 404:
                return "destroyed"
            raise BackendError(f"state failed: {e}") from e
        status = str(getattr(info.status, "value", info.status))
        return _STATES.get(status, status)

    async def close(self) -> None:
        for sid in list(self._conns):
            await self._drop_conn(sid)
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
