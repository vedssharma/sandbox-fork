"""The sandbox backend interface the orchestrator talks to.

Two implementations: `DaytonaBackend` (real Linux VM sandboxes) and `FakeBackend` (in-process, for
tests and demos). Keeping this surface small is what lets us swap or mock Daytona (PLAN.md section 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class BackendError(RuntimeError):
    pass


class ParentHasChildrenError(BackendError):
    """Daytona refuses to delete a sandbox that still has live fork children."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class SandboxBackend(Protocol):
    async def create(self, *, name: str | None = None, snapshot: str | None = None) -> str:
        """Create a started sandbox from `snapshot` (or the default VM snapshot). Returns its id."""
        ...

    async def fork(self, sandbox_id: str, *, name: str | None = None) -> str:
        """Fork disk + memory + processes into a new started sandbox. Returns the child id."""
        ...

    async def pause(self, sandbox_id: str) -> None: ...

    async def resume(self, sandbox_id: str) -> None: ...

    async def hot_snapshot(self, sandbox_id: str, name: str) -> str:
        """Snapshot disk + memory of a started sandbox. Returns the snapshot name."""
        ...

    async def delete_snapshot(self, name: str) -> None: ...

    async def exec(self, sandbox_id: str, command: str, *, timeout: int | None = None) -> ExecResult: ...

    async def delete(self, sandbox_id: str) -> None:
        """Delete a sandbox. Raises ParentHasChildrenError if it still has live forks."""
        ...

    async def state(self, sandbox_id: str) -> str:
        """Backend lifecycle state, e.g. 'started', 'paused', 'destroyed'."""
        ...

    async def close(self) -> None: ...
