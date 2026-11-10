# ruff: noqa: E501
"""Phase 6: compare the repro selector with the default selector on the same branches.

    python bench/phase6/analyze.py OUT_DIR [--phase5 PHASE5_SUMMARY_JSON] > summary.md

OUT_DIR is a bench/phase4/run.py output folder (its summary.json). For every `_repro` run the report
holds, at the fork point, the hidden-test result of every branch (recorded, never used to select),
what the repro selector picked and what the default selector would have picked. So each run gives a
paired comparison of the two selectors on identical branches, plus an "oracle" solve (some branch
passed the hidden tests).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["runs"]


def per_condition(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    for cond, rs in by.items():
        n = len(rs)
        cost = sum(r["cost_usd"] or 0 for r in rs)
        sel = sum(r.get("selection_usd") or 0 for r in rs)
        solved = sum(r["solved"] for r in rs)
        fp = [r for r in rs if "fp_branches" in r]
        any_ok = [r for r in fp if r["fp_any_correct"]]
        solved_default = sum(r.get("solved_default", r["solved"]) for r in rs)
        out[cond] = {
            "runs": n,
            "solved": solved,
            "solved_default": solved_default if fp else None,
            "oracle": sum(r["fp_any_correct"] for r in fp)
            + sum(r["solved"] for r in rs if "fp_branches" not in r)
            if fp
            else None,
            "cost": cost,
            "selection_cost": sel,
            "per_solve": cost / solved if solved else None,
            "per_solve_default": (cost - sel) / solved_default if fp and solved_default else None,
            "fork_points": len(fp),
            "any_ok": len(any_ok),
            "repro_ok": sum(r["repro_pick_ok"] for r in any_ok),
            "default_ok": sum(r["default_pick_ok"] for r in any_ok),
            "differ": sum(r["picks_differ"] for r in fp),
            "differ_repro_right": sum(
                r["picks_differ"] and r["repro_pick_ok"] and not r["default_pick_ok"] for r in fp
            ),
            "differ_default_right": sum(
                r["picks_differ"] and r["default_pick_ok"] and not r["repro_pick_ok"] for r in fp
            ),
            "writer_failed": sum(not r.get("repro_ok") for r in fp),
            "decided_by_repro": sum(r.get("deciding") == "repro" for r in fp),
            "decided_by_judge": sum(r.get("deciding") == "judge" for r in fp),
            "default_by_judge": sum(r.get("default_deciding") == "judge" for r in fp),
        }
    return out


def fmt(x: float | None, d: int = 3) -> str:
    return "-" if x is None else f"{x:.{d}f}"


def render(rows: list[dict[str, Any]], phase5: list[dict[str, Any]] | None) -> str:
    agg = per_condition(rows)
    lines = [
        "# Phase 6 summary: repro selector vs default selector",
        "",
        "## Solves and cost",
        "",
        "`solved` uses the condition's own selector. `default sel.` is what the default selector would "
        "have solved on the same branches (its pick's hidden-test result). `oracle` = some branch passed "
        "the hidden tests. $ includes the repro writer; `$ / solve (default)` leaves the writer out.",
        "",
        "| condition | runs | solved | default sel. | oracle | $ total | writer $ | $ / solve | $ / solve (default) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c, a in agg.items():
        lines.append(
            f"| {c} | {a['runs']} | {a['solved']} | {a['solved_default'] if a['solved_default'] is not None else '-'} | "
            f"{a['oracle'] if a['oracle'] is not None else '-'} | {a['cost']:.2f} | {a['selection_cost']:.2f} | "
            f"{fmt(a['per_solve'])} | {fmt(a['per_solve_default'])} |"
        )
    lines += [
        "",
        "## Selection at the fork point",
        "",
        "| condition | fork points | with a correct branch | repro picked a correct one | default picked a correct one | picks differ | ...repro right | ...default right | writer failed | decided by repro | by judge (repro / default) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c, a in agg.items():
        if not a["fork_points"]:
            continue
        lines.append(
            f"| {c} | {a['fork_points']} | {a['any_ok']} | {a['repro_ok']}/{a['any_ok']} | {a['default_ok']}/{a['any_ok']} | "
            f"{a['differ']} | {a['differ_repro_right']} | {a['differ_default_right']} | {a['writer_failed']} | "
            f"{a['decided_by_repro']} | {a['decided_by_judge']} / {a['default_by_judge']} |"
        )
    tasks = list(dict.fromkeys(r["task"] for r in rows))
    conds = list(agg)
    lines += ["", "## Per task (solved / default sel. / oracle, out of runs)", ""]
    lines.append("| task | " + " | ".join(conds) + " |")
    lines.append("|---|" + "---|" * len(conds))
    for t in tasks:
        cells = []
        for c in conds:
            rs = [r for r in rows if r["task"] == t and r["condition"] == c]
            if not rs:
                cells.append("-")
                continue
            s = sum(r["solved"] for r in rs)
            if any("fp_branches" in r for r in rs):
                d = sum(r.get("solved_default", r["solved"]) for r in rs)
                o = sum(r["fp_any_correct"] if "fp_branches" in r else r["solved"] for r in rs)
                cells.append(f"{s} / {d} / {o} of {len(rs)}")
            else:
                cells.append(f"{s} of {len(rs)}")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    if phase5:
        p5 = [r for r in phase5 if r["task"] in tasks]
        a5 = per_condition(p5)
        lines += ["", "## Phase 5 on the same tasks (default selector, independent runs)", ""]
        lines += ["| condition | runs | solved | $ total | $ / solve |", "|---|---|---|---|---|"]
        for c, a in a5.items():
            lines.append(f"| {c} | {a['runs']} | {a['solved']} | {a['cost']:.2f} | {fmt(a['per_solve'])} |")
    lines += ["", "## Runs", ""]
    lines.append(
        "| task | condition | rep | solved | default sel. | branches ok | checks (fail at fork pt) | deciding | default deciding | writer $ | $ |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "fp_branches" not in r:
            lines.append(
                f"| {r['task']} | {r['condition']} | {r['rep']} | {'yes' if r['solved'] else 'no'} | - | - | - | - | - | - | {r['cost_usd']} |"
            )
            continue
        lines.append(
            f"| {r['task']} | {r['condition']} | {r['rep']} | {'yes' if r['solved'] else 'no'} | "
            f"{'yes' if r.get('solved_default') else 'no'} | {r['fp_correct_branches']}/{r['fp_branches']} | "
            f"{r['repro_checks']} ({r['repro_failing_at_fork_point']}) | {r['deciding']} | {r['default_deciding']} | "
            f"{fmt(r.get('repro_writer_usd'))} | {r['cost_usd']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="+", help="run.py output folder(s)")
    ap.add_argument("--phase5", help="Phase 5 summary.json, for the same tasks under the default selector")
    args = ap.parse_args()
    rows = [r for o in args.out for r in load(Path(o) / "summary.json")]
    phase5 = load(Path(args.phase5)) if args.phase5 else None
    print(render(rows, phase5), end="")


if __name__ == "__main__":
    main()
