"""Store every evaluation's raw outputs so a human can audit why a branch won (PLAN.md 6).

Layout under the store's root (local directory for now; object storage later):

    <run_id>/<fork_point>/
        selection.json              ranking, components, deciding factor, judge verdict, explanation
        <branch>/evaluation.json    the Evaluation (without the full outputs)
        <branch>/tests.log  junit.xml  bench-<name>-<i>.log  gate-<name>.log  lint.log  diff.patch
        baseline/...                the fork point, evaluated the same way
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .evaluator import Evaluation
from .scoring import Selection


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save_selection(self, run_id: str, fork_point: str, selection: Selection) -> Path:
        d = self.root / _safe(run_id) / _safe(fork_point)
        d.mkdir(parents=True, exist_ok=True)
        evals = list(selection.ranked)
        if selection.baseline is not None:
            self.save_evaluation(d / "baseline", selection.baseline)
        for e in evals:
            self.save_evaluation(d / _safe(e.label or e.node_id), e)
        (d / "selection.json").write_text(json.dumps(selection.as_dict(), indent=2, default=str))
        return d

    def save_evaluation(self, d: Path, e: Evaluation) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        for name, content in e.artifacts.items():
            (d / _safe(name)).write_text(content)
        e.artifact_dir = str(d)
        data: dict[str, Any] = e.as_dict()
        data.pop("raw", None)  # full copies are the files next to it
        data["artifacts"] = sorted(e.artifacts)
        (d / "evaluation.json").write_text(json.dumps(data, indent=2, default=str))
        return d


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "x"


__all__ = ["ArtifactStore"]
