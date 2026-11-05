"""Core data model for the execution tree (PLAN.md section 3.1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    return datetime.now(UTC)


class NodeKind(StrEnum):
    ROOT = "root"
    BRANCH = "branch"
    CHECKPOINT = "checkpoint"


class NodeStatus(StrEnum):
    RUNNING = "running"  # live sandbox, being worked on
    PAUSED = "paused"  # live sandbox, frozen (checkpoint or parked branch)
    EVALUATING = "evaluating"
    WON = "won"
    LOST = "lost"  # marked for deletion; sandbox may still exist until GC reaches it
    FAILED = "failed"
    GC = "gc"  # sandbox deleted; row kept for history

    @property
    def has_sandbox(self) -> bool:
        """Whether a node in this status may still own a Daytona sandbox."""
        return self is not NodeStatus.GC


LIVE_WORK_STATUSES = frozenset({NodeStatus.RUNNING, NodeStatus.EVALUATING, NodeStatus.WON})


class RunStatus(StrEnum):
    ACTIVE = "active"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass
class Run:
    task: str
    id: str = field(default_factory=lambda: new_id("run"))
    head_node_id: str | None = None
    status: RunStatus = RunStatus.ACTIVE
    created_at: datetime = field(default_factory=now)


@dataclass
class Node:
    run_id: str
    sandbox_id: str
    kind: NodeKind
    parent_id: str | None = None
    status: NodeStatus = NodeStatus.RUNNING
    depth: int = 0
    hypothesis: str | None = None  # the approach this branch is trying
    label: str | None = None  # checkpoint label, if any
    agent_state_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("node"))
    created_at: datetime = field(default_factory=now)
