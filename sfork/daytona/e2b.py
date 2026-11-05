"""E2B backend.

Reads E2B_API_KEY (and optionally E2B_DOMAIN) from the environment via the SDK. The network policy
must allow `api.e2b.app` (control plane) and `sandbox.e2b.app` (exec / files inside sandboxes).

How our protocol maps onto E2B (SDK `e2b` 2.63):
- `fork` -> `AsyncSandbox.fork(sandbox_id, count=1)`. E2B checkpoints the parent in place (brief
  pause, full memory + disk snapshot, resume) and boots the child from that checkpoint. The parent's
  open command streams drop during the pause, which is fine for us because every `exec` is a fresh
  request. `fork_many` makes N children from a single checkpoint (up to 20 per call).
- E2B only forks running sandboxes, so forking a paused checkpoint resumes it, forks, and pauses it
  again. That keeps the engine's "checkpoints are paused" model intact. Set `auto_resume_paused`
  to False to see the raw API behaviour (a 409).
- E2B rejects a fork while the same parent is still being checkpointed for another fork (409
  "snapshotting"), so forks of one parent are serialized with a per-parent lock.
- A snapshot can't be deleted while sandboxes created from it are still running (400); delete
  those sandboxes first.
- `pause` / `resume` -> `pause(mode="full")` / `connect()`; memory and processes survive.
- `hot_snapshot` -> `create_snapshot(name, mode="full")`; returns the snapshot id, which `create`
  accepts in place of a template name. Snapshots survive deletion of the source sandbox.
- Children are independent of their parent, so `delete` never raises `ParentHasChildrenError`.
- E2B sandboxes die when their timeout runs out (300 s by default). We set it explicitly; the Hobby
  plan allows at most 3600 s of continuous runtime, Pro 86400 s.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .base import BackendError, ExecResult

T = TypeVar("T")

DEFAULT_TEMPLATE = os.environ.get("SFORK_E2B_TEMPLATE", "base")
DEFAULT_TIMEOUT = int(os.environ.get("SFORK_E2B_TIMEOUT", "3600"))
MAX_FORKS_PER_CALL = 20


class E2BBackend:
    def __init__(
        self,
        *,
        default_template: str = DEFAULT_TEMPLATE,
        sandbox_timeout: int = DEFAULT_TIMEOUT,
        exec_user: str = "root",
        max_retries: int = 6,
        metadata: dict[str, str] | None = None,
        sandbox_cls: Any = None,
    ) -> None:
        if sandbox_cls is None:
            from e2b import AsyncSandbox

            sandbox_cls = AsyncSandbox
        self._cls = sandbox_cls
        self._default_template = default_template
        self._timeout = sandbox_timeout
        # The post-fork hook rewrites machine-id and hostname, so commands run as root by default.
        self._user = exec_user
        self._max_retries = max_retries
        self._metadata = {"sfork": "1", **(metadata or {})}
        self._cache: dict[str, Any] = {}  # sandbox id -> AsyncSandbox
        self._paused: set[str] = set()
        self._fork_locks: dict[str, asyncio.Lock] = {}
        self.auto_resume_paused = True

    # helpers ----------------------------------------------------------------
    async def _retry(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Retry on 429 with jittered exponential backoff; translate other errors."""
        from e2b import RateLimitException, SandboxException

        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                return await fn()
            except RateLimitException:
                if attempt == self._max_retries:
                    raise
            except SandboxException as e:
                raise BackendError(f"{op} failed: {e}") from e
            await asyncio.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    async def _sb(self, sandbox_id: str) -> Any:
        sb = self._cache.get(sandbox_id)
        if sb is None:
            # connect() resumes a paused sandbox, so only use it for ones we know are running.
            sb = await self._retry("connect", lambda: self._cls.connect(sandbox_id, timeout=self._timeout))
            self._cache[sandbox_id] = sb
            self._paused.discard(sandbox_id)
        return sb

    # SandboxBackend ---------------------------------------------------------
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        meta = {**self._metadata, **({"name": name} if name else {})}
        template = snapshot or self._default_template
        sb = await self._retry(
            "create", lambda: self._cls.create(template, timeout=self._timeout, metadata=meta)
        )
        self._cache[sb.sandbox_id] = sb
        return sb.sandbox_id

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        return (await self.fork_many(sandbox_id, 1))[0]

    async def fork_many(self, sandbox_id: str, count: int) -> list[str]:
        """Fork `count` children from one checkpoint of `sandbox_id`. All-or-nothing."""
        if count < 1:
            return []
        lock = self._fork_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            return await self._fork_many_locked(sandbox_id, count)

    async def _fork_many_locked(self, sandbox_id: str, count: int) -> list[str]:
        repause = self.auto_resume_paused and sandbox_id in self._paused
        if repause:
            await self.resume(sandbox_id)
        made: list[str] = []
        errors: list[Exception] = []
        try:
            remaining = count
            while remaining:
                n = min(remaining, MAX_FORKS_PER_CALL)
                res = await self._retry(
                    "fork", lambda n=n: self._cls.fork(sandbox_id, timeout=self._timeout, count=n)
                )
                for r in res:
                    if isinstance(r, Exception):
                        errors.append(r)
                    else:
                        self._cache[r.sandbox_id] = r
                        made.append(r.sandbox_id)
                remaining -= n
                if errors:
                    break
        finally:
            if repause:
                await self.pause(sandbox_id)
        if errors:
            for sid in made:
                try:
                    await self.delete(sid)
                except BackendError:
                    pass
            raise BackendError(f"fork failed: {errors[0]}") from errors[0]
        return made

    async def pause(self, sandbox_id: str) -> None:
        await self._retry("pause", lambda: self._cls.pause(sandbox_id, mode="full"))
        self._cache.pop(sandbox_id, None)
        self._paused.add(sandbox_id)

    async def resume(self, sandbox_id: str) -> None:
        sb = await self._retry("resume", lambda: self._cls.connect(sandbox_id, timeout=self._timeout))
        self._cache[sandbox_id] = sb
        self._paused.discard(sandbox_id)

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        info = await self._retry(
            "hot_snapshot", lambda: self._cls.create_snapshot(sandbox_id, name, mode="full")
        )
        return info.snapshot_id

    async def delete_snapshot(self, name: str) -> None:
        await self._retry("snapshot_delete", lambda: self._cls.delete_snapshot(name))

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        from e2b import CommandExitException

        sb = await self._sb(sandbox_id)

        async def run() -> ExecResult:
            try:
                res = await sb.commands.run(command, user=self._user, timeout=timeout or 0)
            except CommandExitException as e:
                res = e
            return ExecResult(exit_code=res.exit_code, output=(res.stdout or "") + (res.stderr or ""))

        return await self._retry("exec", run)

    async def delete(self, sandbox_id: str) -> None:
        await self._retry("delete", lambda: self._cls.kill(sandbox_id))
        self._cache.pop(sandbox_id, None)
        self._paused.discard(sandbox_id)
        self._fork_locks.pop(sandbox_id, None)

    async def state(self, sandbox_id: str) -> str:
        from e2b import NotFoundException

        try:
            info = await self._cls.get_info(sandbox_id)
        except NotFoundException:
            return "destroyed"
        state = str(getattr(info.state, "value", info.state))
        # Use Daytona's vocabulary so callers see the same states from every backend.
        return {"running": "started"}.get(state, state)

    async def close(self) -> None:
        self._cache.clear()
