"""Tree store. SQLite for local dev and tests; the same schema ports to Postgres later."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Node, NodeKind, NodeStatus, Run, RunStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    head_node_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    parent_id TEXT REFERENCES nodes(id),
    sandbox_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    depth INTEGER NOT NULL,
    hypothesis TEXT,
    label TEXT,
    agent_state_ref TEXT,
    meta TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS nodes_run ON nodes(run_id);
"""


class TreeStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # runs -----------------------------------------------------------------
    def put_run(self, run: Run) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
            (run.id, run.task, run.head_node_id, run.status.value, run.created_at.isoformat()),
        )
        self._db.commit()

    def get_run(self, run_id: str) -> Run:
        row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return Run(
            id=row["id"],
            task=row["task"],
            head_node_id=row["head_node_id"],
            status=RunStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_runs(self) -> list[Run]:
        ids = [r["id"] for r in self._db.execute("SELECT id FROM runs ORDER BY created_at")]
        return [self.get_run(i) for i in ids]

    # nodes ----------------------------------------------------------------
    def put_node(self, node: Node) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node.id,
                node.run_id,
                node.parent_id,
                node.sandbox_id,
                node.kind.value,
                node.status.value,
                node.depth,
                node.hypothesis,
                node.label,
                node.agent_state_ref,
                json.dumps(node.meta),
                node.created_at.isoformat(),
            ),
        )
        self._db.commit()

    def get_node(self, node_id: str) -> Node:
        row = self._db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(node_id)
        return self._row_to_node(row)

    def children(self, node_id: str) -> list[Node]:
        rows = self._db.execute(
            "SELECT * FROM nodes WHERE parent_id=? ORDER BY created_at, id", (node_id,)
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def run_nodes(self, run_id: str) -> list[Node]:
        rows = self._db.execute(
            "SELECT * FROM nodes WHERE run_id=? ORDER BY depth, created_at, id", (run_id,)
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def ancestors(self, node_id: str) -> list[Node]:
        """Ancestors of a node, nearest first (recursive CTE, ports to Postgres as is)."""
        rows = self._db.execute(
            """
            WITH RECURSIVE anc(id, parent_id, lvl) AS (
                SELECT id, parent_id, 0 FROM nodes WHERE id = ?
                UNION ALL
                SELECT n.id, n.parent_id, anc.lvl + 1 FROM nodes n JOIN anc ON n.id = anc.parent_id
            )
            SELECT nodes.* FROM nodes JOIN anc ON nodes.id = anc.id WHERE anc.lvl > 0 ORDER BY anc.lvl
            """,
            (node_id,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            run_id=row["run_id"],
            parent_id=row["parent_id"],
            sandbox_id=row["sandbox_id"],
            kind=NodeKind(row["kind"]),
            status=NodeStatus(row["status"]),
            depth=row["depth"],
            hypothesis=row["hypothesis"],
            label=row["label"],
            agent_state_ref=row["agent_state_ref"],
            meta=json.loads(row["meta"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
