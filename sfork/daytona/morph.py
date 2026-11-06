"""Morph Cloud backend.

Reads MORPH_API_KEY from the environment via the SDK. The network policy must allow
`cloud.morph.so` (the SDK sends create, branch, snapshot, exec and delete to
`https://cloud.morph.so/api`).

How our protocol maps onto Morph Cloud (SDK `morphcloud` 0.1.116):
- `create` -> `instances.astart(snapshot_id)`. Morph boots instances from snapshots only, so with no
  `snapshot` we build (once per account, keyed by a digest) a base snapshot from the
  `morphvm-minimal` image at SFORK_MORPH_VCPUS / SFORK_MORPH_MEMORY / SFORK_MORPH_DISK.
- `fork` / `fork_many` -> `Instance.abranch(count=N)`: Morph snapshots the running instance (memory,
  disk and processes) and starts N instances from that snapshot. We keep the branch snapshot until
  every child started from it is deleted, then remove it.
- Morph only branches ready instances, so forking a paused checkpoint resumes it, branches, and
  pauses it again (same contract as the E2B backend). Forks of one parent are serialized.
- `pause` / `resume` -> `apause()` / `aresume()`; memory and processes survive. Pause is slow
  (about 6 s in the 2026-11-06 spike), so forking a paused checkpoint costs about 11 s.
- `hot_snapshot` -> `asnapshot(metadata={"name": ...})`; returns the snapshot id, which `create`
  accepts in place of a template.
- Children are independent instances, so `delete` never raises `ParentHasChildrenError`.
- Instances get a TTL (SFORK_MORPH_TTL seconds, default 3600) with action "stop" so a crashed run
  doesn't keep billing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .base import BackendError, ExecResult

T = TypeVar("T")

DEFAULT_IMAGE = os.environ.get("SFORK_MORPH_IMAGE", "morphvm-minimal")
DEFAULT_VCPUS = int(os.environ.get("SFORK_MORPH_VCPUS", "2"))
DEFAULT_MEMORY = int(os.environ.get("SFORK_MORPH_MEMORY", "2048"))  # MiB
DEFAULT_DISK = int(os.environ.get("SFORK_MORPH_DISK", "8192"))  # MiB
DEFAULT_TTL = int(os.environ.get("SFORK_MORPH_TTL", "3600"))
READY_TIMEOUT = 300.0

# Morph reports instance status in its own words; map them onto Daytona's vocabulary.
_STATES = {"ready": "started", "paused": "paused", "pending": "pending", "saving": "saving", "error": "error"}


class MorphBackend:
    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        vcpus: int = DEFAULT_VCPUS,
        memory: int = DEFAULT_MEMORY,
        disk_size: int = DEFAULT_DISK,
        ttl_seconds: int = DEFAULT_TTL,
        max_retries: int = 6,
        metadata: dict[str, str] | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            from morphcloud.api import MorphCloudClient

            client = MorphCloudClient()
        self._client = client
        self._image, self._vcpus, self._memory, self._disk = image, vcpus, memory, disk_size
        self._ttl = ttl_seconds
        self._max_retries = max_retries
        self._metadata = {"sfork": "1", **(metadata or {})}
        self._cache: dict[str, Any] = {}  # instance id -> Instance
        self._paused: set[str] = set()
        self._fork_locks: dict[str, asyncio.Lock] = {}
        # Branch snapshots: snapshot id -> ids of live children started from it.
        self._branch_children: dict[str, set[str]] = {}
        self._branch_of: dict[str, str] = {}  # child id -> branch snapshot id
        self._base_snapshot: str | None = None
        # Snapshots we must never delete as a side effect of deleting a fork: the base, snapshots
        # instances were started from, and hot snapshots (those are deleted via delete_snapshot).
        self._protected: set[str] = set()
        self.auto_resume_paused = True

    # helpers ----------------------------------------------------------------
    async def _retry(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Retry on 429 / 5xx with jittered exponential backoff; translate other errors."""
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                return await fn()
            except BackendError:
                raise
            except Exception as e:  # noqa: BLE001 - the SDK raises ApiError, httpx and plain errors
                status = getattr(e, "status_code", None)
                retryable = status == 429 or (isinstance(status, int) and status >= 500)
                if not retryable or attempt == self._max_retries:
                    raise BackendError(f"{op} failed: {e}") from e
            await asyncio.sleep(delay + random.random() * delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    async def _inst(self, instance_id: str) -> Any:
        inst = self._cache.get(instance_id)
        if inst is None:
            inst = await self._retry("get", lambda: self._client.instances.aget(instance_id))
            self._cache[instance_id] = inst
        return inst

    async def _base(self) -> str:
        if self._base_snapshot:
            return self._base_snapshot
        key = f"{self._image}:{self._vcpus}:{self._memory}:{self._disk}"
        digest = "sfork-base-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        existing = await self._retry("snapshot_list", lambda: self._client.snapshots.alist(digest=digest))
        if existing:
            snap = existing[0]
        else:
            snap = await self._retry(
                "snapshot_create",
                lambda: self._client.snapshots.acreate(
                    image_id=self._image,
                    vcpus=self._vcpus,
                    memory=self._memory,
                    disk_size=self._disk,
                    digest=digest,
                    metadata={"sfork": "base"},
                ),
            )
        self._base_snapshot = snap.id
        self._protected.add(snap.id)
        return snap.id

    # SandboxBackend ---------------------------------------------------------
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        snapshot_id = snapshot or await self._base()
        self._protected.add(snapshot_id)
        meta = {**self._metadata, **({"name": name} if name else {})}
        inst = await self._retry(
            "create",
            lambda: self._client.instances.astart(
                snapshot_id, metadata=meta, ttl_seconds=self._ttl, ttl_action="stop", timeout=READY_TIMEOUT
            ),
        )
        self._cache[inst.id] = inst
        return inst.id

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        return (await self.fork_many(sandbox_id, 1))[0]

    async def fork_many(self, sandbox_id: str, count: int) -> list[str]:
        """Branch `count` children from one snapshot of `sandbox_id`. All-or-nothing."""
        if count < 1:
            return []
        lock = self._fork_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            repause = self.auto_resume_paused and sandbox_id in self._paused
            if repause:
                await self.resume(sandbox_id)
            try:
                inst = await self._inst(sandbox_id)
                snap, children = await self._retry("fork", lambda: inst.abranch(count))
            finally:
                if repause:
                    await self.pause(sandbox_id)
        ids = [c.id for c in children]
        for c in children:
            self._cache[c.id] = c
        # Morph's branch response names the snapshot the parent was started from (observed live:
        # it is the base snapshot), not a fresh one, so only track it for cleanup when it is new.
        if snap.id not in self._protected:
            for cid in ids:
                self._branch_of[cid] = snap.id
            self._branch_children.setdefault(snap.id, set()).update(ids)
        if len(ids) != count:
            for cid in ids:
                try:
                    await self.delete(cid)
                except BackendError:
                    pass
            raise BackendError(f"fork failed: asked for {count} children, Morph started {len(ids)}")
        return ids

    async def pause(self, sandbox_id: str) -> None:
        inst = await self._inst(sandbox_id)
        await self._retry("pause", inst.apause)
        self._paused.add(sandbox_id)

    async def resume(self, sandbox_id: str) -> None:
        inst = await self._inst(sandbox_id)
        await self._retry("resume", inst.aresume)
        await self._retry("resume", lambda: inst.await_until_ready(timeout=READY_TIMEOUT))
        self._paused.discard(sandbox_id)

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        inst = await self._inst(sandbox_id)
        snap = await self._retry(
            "hot_snapshot", lambda: inst.asnapshot(metadata={**self._metadata, "name": name})
        )
        await self._retry("hot_snapshot", lambda: snap.await_until_ready(timeout=READY_TIMEOUT))
        self._protected.add(snap.id)
        return snap.id

    async def delete_snapshot(self, name: str) -> None:
        snap = await self._retry("snapshot_get", lambda: self._client.snapshots.aget(name))
        await self._retry("snapshot_delete", snap.adelete)

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult:
        inst = await self._inst(sandbox_id)
        res = await self._retry(
            # Morph joins argv and runs it under `bash -c`, so the command must be one string:
            # ["sh", "-c", cmd] would become `bash -c "sh -c cmd..."` and lose everything after word one.
            "exec",
            lambda: inst.aexec(command, timeout=float(timeout) if timeout else None),
        )
        return ExecResult(exit_code=res.exit_code, output=(res.stdout or "") + (res.stderr or ""))

    async def delete(self, sandbox_id: str) -> None:
        await self._retry("delete", lambda: self._client.instances.astop(sandbox_id))
        self._cache.pop(sandbox_id, None)
        self._paused.discard(sandbox_id)
        self._fork_locks.pop(sandbox_id, None)
        snap_id = self._branch_of.pop(sandbox_id, None)
        if snap_id is not None:
            live = self._branch_children.get(snap_id, set())
            live.discard(sandbox_id)
            if not live:
                self._branch_children.pop(snap_id, None)
                try:
                    await self.delete_snapshot(snap_id)
                except BackendError:
                    pass  # a leftover branch snapshot only costs storage

    async def state(self, sandbox_id: str) -> str:
        try:
            inst = await self._client.instances.aget(sandbox_id)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:
                return "destroyed"
            raise BackendError(f"state failed: {e}") from e
        self._cache[sandbox_id] = inst
        status = str(getattr(inst.status, "value", inst.status))
        return _STATES.get(status, status)

    async def close(self) -> None:
        self._cache.clear()
