"""Checkpoint and rewind in the agent loop: scripted model, real shell (LocalBackend). No network.

The mini task mirrors examples/billing_migration: a 'migration' rewrites a data file in place and
drops middle names, which can't be repaired afterwards, so the agent has to rewind.
"""

import asyncio
from pathlib import Path

import pytest

from sfork.agent.llm import block_get, text, tool_use
from sfork.agent.policy import CheckpointPolicy
from sfork.agent.runtime import REWIND_TAG
from sfork.core.models import NodeStatus
from sfork.eval.config import EvalConfig, TestSpec
from sfork.orchestrator.explore import Task
from tests.test_explore import assert_well_formed, make

NAMES = "Mary Ann Smith\nCher\nAva Lee\nJose Luis Ramos Ortiz\n"
BUGGY = """
rows = [l.split() for l in open("data.txt").read().splitlines()]
open("data.txt", "w").write("".join(f"{r[0]}|{r[-1]}\\n" for r in rows))
"""
FIXED = """
out = []
for l in open("data.txt").read().splitlines():
    head, _, tail = l.rpartition(" ")
    out.append(f"{head}|{tail}" if head else f"{l}|")
open("data.txt", "w").write("".join(x + "\\n" for x in out))
"""
CHECK = """
import json
want = open(".expected").read().splitlines()
got = [" ".join(p for p in l.split("|") if p) for l in open("data.txt").read().splitlines()]
split = all("|" in l for l in open("data.txt").read().splitlines())
ok = [split, got == want]
print(json.dumps({"passed": sum(ok), "total": len(ok)}))
"""


def mini_task(**kw) -> Task:
    ev = EvalConfig(
        workdir="/workspace",
        setup=(f"printf '{NAMES}' > data.txt && cp data.txt .expected",),
        tests=TestSpec(cmd="python3 check.py", parse="json", timeout=60),
    )
    return Task(
        name="mini_migration",
        prompt="Run the migration and make check.py pass.",
        eval=ev,
        files={"migrate.py": BUGGY, "check.py": CHECK},
        **kw,
    )


def rewound(messages) -> int:
    """How many rewinds this branch has done (the latest rewind note says; older ones were cut)."""
    for m in reversed(messages):
        c = block_get(m, "content")
        for b in c if isinstance(c, list) else []:
            t = block_get(b, "text") or ""
            if REWIND_TAG in t:
                return int(t.split("rewind #", 1)[1].split(" ", 1)[0])
    return 0


def last_checkpoint(messages) -> str:
    for m in reversed(messages):
        c = block_get(m, "content")
        for b in c if isinstance(c, list) else []:
            t = block_get(b, "content")
            if isinstance(t, str) and "checkpoint node_" in t:
                return "node_" + t.split("checkpoint node_", 1)[1].split(" ", 1)[0].split("'", 1)[0]
    raise AssertionError("no checkpoint in history")


def recover_script(branch, turn, messages):
    """Run the buggy migration (auto-checkpointed), see the damage, rewind, fix, rerun, finish."""
    if not rewound(messages):
        if turn == 0:
            return [tool_use("bash", {"command": "python3 migrate.py"})]
        if turn == 1:
            return [tool_use("run_tests", {})]
        cp = last_checkpoint(messages)
        return [
            text("Middle names are gone and can't be recovered. Rewinding."),
            tool_use("rewind", {"checkpoint_id": cp, "lesson": "split on the LAST space"}),
        ]
    return [
        [tool_use("write_file", {"path": "migrate.py", "content": FIXED})],
        [tool_use("bash", {"command": "python3 migrate.py"})],
        [tool_use("run_tests", {})],
        [tool_use("done", {"summary": "fixed the split"})],
    ][turn - 1]  # the kept prefix already holds one assistant turn (the first migrate call)


def test_auto_checkpoint_then_rewind_recovers_destroyed_data():
    explorer, backend, llm, events = make(recover_script)
    report = asyncio.run(explorer.run(mini_task()))

    assert report["status"] == "ok"
    ev = report["final_evaluation"]
    assert ev["passed"] == ev["total"] == 2
    # two auto-checkpoints (one per migrate run), one rewind to the first
    assert [c["auto"] for c in report["checkpoints"]] == [True, True]
    assert all(c["label"] == "before: python3 migrate.py" for c in report["checkpoints"])
    (rw,) = report["rewinds"]
    assert rw["checkpoint"] == report["checkpoints"][0]["checkpoint"]
    assert rw["lesson"] == "split on the LAST space"

    br = explorer._branches[0][0]
    assert br.rewinds == 1 and br.lessons == ["split on the LAST space"]
    assert_well_formed(br.messages)
    # history: task, the first migrate turn (kept), then the rewind turn answering it
    assert br.messages[0]["content"] == "Run the migration and make check.py pass."
    turn = br.messages[2]["content"]
    assert turn[0]["is_error"] and "Not run: you rewound" in turn[0]["content"]
    assert REWIND_TAG in turn[-1]["text"] and "split on the LAST space" in turn[-1]["text"]
    assert not any("Middle names are gone" in str(m) for m in br.messages)  # abandoned turns cut

    store = explorer.engine.store
    abandoned = store.get_node(rw["abandoned_node"])
    assert abandoned.status is NodeStatus.GC and abandoned.parent_id == rw["checkpoint"]
    assert store.get_node(rw["new_node"]).meta["lesson"] == "split on the LAST space"
    assert backend.live_ids() == set() and report["leftover_sandboxes"] == []
    assert "migrate.py" in report["diff"] and "rpartition" in report["diff"]
    assert len(report["transcripts"]["root"]) == len(br.messages)
    kinds = [e["type"] for e in events]
    assert kinds.count("checkpointed") == 2 and kinds.count("rewound") == 1


def test_manual_checkpoints_rewind_twice_and_refusals():
    """checkpoint A, checkpoint B, rewind to A twice; B is forgotten once you rewind past it."""

    def script(branch, turn, messages):
        n = rewound(messages)
        if n == 0:
            return [
                [tool_use("checkpoint", {"label": "A"})],
                [
                    tool_use("bash", {"command": "echo one > note.txt"}),
                    tool_use("checkpoint", {"label": "B"}),
                ],
                [tool_use("rewind", {"checkpoint_id": "node_nope", "lesson": "x"})],
                [tool_use("rewind", {"checkpoint_id": cps.get("A"), "lesson": "first"})],
            ][turn]
        if n == 1:
            if turn == 1:  # B belonged to the abandoned timeline
                return [tool_use("rewind", {"checkpoint_id": cps["B"], "lesson": "x"})]
            if turn == 2:
                return [tool_use("bash", {"command": "test ! -e note.txt && echo two > note.txt"})]
            return [tool_use("rewind", {"checkpoint_id": cps["A"], "lesson": "second"})]
        return [tool_use("bash", {"command": "test ! -e note.txt"}), tool_use("done", {"summary": "ok"})]

    cps: dict[str, str] = {}

    def on_event(e):
        if e["type"] == "checkpoint":
            cps[e["label"]] = e["checkpoint"]

    explorer, backend, _, events = make(script)
    explorer.cfg.max_steps_root = 12
    explorer.on_event = lambda e: (on_event(e), events.append(e))
    report = asyncio.run(explorer.run(mini_task()))

    assert report["status"] == "ok", report.get("error")
    refused = [e for e in events if e["type"] == "rewind_refused"]
    assert len(refused) == 2 and all("not a checkpoint" in r["reason"] for r in refused)
    assert [r["lesson"] for r in report["rewinds"]] == ["first", "second"]
    assert {r["checkpoint"] for r in report["rewinds"]} == {cps["A"]}
    br = explorer._branches[0][0]
    assert br.lessons == ["first", "second"] and [c.label for c in br.checkpoints] == ["A"]
    assert_well_formed(br.messages)
    # the last bash (note.txt absent) ran in the final timeline and succeeded
    assert report["final_summary"] == "ok"
    tail = br.messages[-1]["content"]
    assert tail[0]["content"].startswith("exit code 0")
    assert backend.live_ids() == set()


def test_manual_checkpoint_right_before_a_risky_command_is_not_doubled():
    def script(branch, turn, messages):
        if turn == 0:
            return [
                tool_use("checkpoint", {"label": "before migrating"}),
                tool_use("bash", {"command": "python3 migrate.py"}),
                tool_use("bash", {"command": "rm -rf build"}),
            ]
        return [tool_use("done", {"summary": "ok"})]

    explorer, backend, _, _ = make(script)
    report = asyncio.run(explorer.run(mini_task()))
    assert [(c["label"], c["auto"]) for c in report["checkpoints"]] == [
        ("before migrating", False),
        ("before: rm -rf build", True),
    ]
    assert backend.live_ids() == set()


def test_checkpoints_disabled_hides_tools_and_skips_auto_checkpoint():
    def script(branch, turn, messages):
        return [
            [tool_use("bash", {"command": "python3 migrate.py"})],
            [tool_use("done", {"summary": "ran it"})],
        ][turn]

    explorer, backend, llm, _ = make(script, checkpoints=False)
    report = asyncio.run(explorer.run(mini_task()))
    assert report["checkpoints"] == [] and report["rewinds"] == []
    assert "rewind" not in explorer.agent.system
    assert {t["name"] for t in explorer.agent.tools}.isdisjoint({"checkpoint", "rewind"})
    assert backend.live_ids() == set()


def test_auto_checkpoint_can_be_turned_off_per_task():
    def script(branch, turn, messages):
        return [
            [tool_use("bash", {"command": "python3 migrate.py"})],
            [tool_use("done", {"summary": "ran it"})],
        ][turn]

    explorer, _, _, _ = make(script)
    report = asyncio.run(explorer.run(mini_task(checkpoint_policy=None)))
    assert report["checkpoints"] == []
    assert "rewind" in {t["name"] for t in explorer.agent.tools}


def test_rewind_inside_a_forked_branch():
    def script(branch, turn, messages):
        if branch is None:
            alts = [{"name": "careful", "plan": "recover"}, {"name": "hasty", "plan": "ship it"}]
            return [tool_use("fork", {"alternatives": alts})]
        if branch == "hasty":
            return [
                [tool_use("bash", {"command": "python3 migrate.py"})],
                [tool_use("done", {"summary": "shipped"})],
            ][turn]
        # careful: same as recover_script, but turns count from the fork
        return recover_script(branch, turn, messages)

    explorer, backend, _, _ = make(script)
    report = asyncio.run(explorer.run(mini_task()))
    assert report["head_branch"] == "careful"
    assert report["final_evaluation"]["passed"] == 2
    assert [r["branch"] for r in report["rewinds"]] == ["careful"]
    assert {c["branch"] for c in report["checkpoints"]} == {"careful", "hasty"}
    store = explorer.engine.store
    assert all(n.status is NodeStatus.GC for n in store.run_nodes(report["run_id"]))
    assert backend.live_ids() == set()


@pytest.mark.parametrize(
    "cmd",
    [
        "./migrate.sh",
        "cd /app && python3 manage.py migrate",
        "alembic upgrade head",
        "bin/rails db:migrate",
        "psql -c 'ALTER TABLE customers DROP COLUMN name'",
        "psql -f migrations/0003.sql",
        "cat fix.sql | psql",
        "psql < fix.sql",
        "rm -rf build",
        "git reset --hard HEAD~1",
        "python3 migrate.py",
        "cd /workspace && ./migrate.sh",
        "psql -v ON_ERROR_STOP=1 <<'SQL'\nUPDATE invoices SET amount = 0;\nSQL",
        "sudo -u postgres psql < dump.sql",
    ],
)
def test_policy_flags_risky_commands(cmd):
    assert CheckpointPolicy().match(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "cat migrate.sh",
        "cat migrations/0003_split_customer_name.sql",
        "grep -n DROP migrations/*.sql | head",
        "psql -c 'SELECT count(*) FROM customers'",
        "ls migrations",
        "python3 tests/test_billing.py",
        "git diff",
        "cd /workspace; ls -R | head -50; cat migrate.sh; ls migrations; cat migrations/0003*",
        "cat > migrations/0004.sql <<'EOF'\nALTER TABLE invoices DROP COLUMN amount;\nEOF",
        "psql -c '\\d invoices'",
        'psql -Atc "select count(*), min(amount) from invoices"',
        'psql -qAtX -c "SELECT version FROM schema_migrations"',
    ],
)
def test_policy_skips_reads(cmd):
    assert CheckpointPolicy().match(cmd) is None


def test_policy_config():
    assert CheckpointPolicy.from_config({"auto": False}) is None
    p = CheckpointPolicy.from_config({"before": [r"\bdeploy\b"]})
    assert p.match("./deploy prod") and not p.match("./migrate.sh")
    p = CheckpointPolicy.from_config({"also_before": [r"\bdeploy\b"]})
    assert p.match("./deploy prod") and p.match("./migrate.sh")


def test_billing_migration_task_loads():
    task = Task.load(Path(__file__).resolve().parents[1] / "examples" / "billing_migration")
    assert "migrate.sh" in task.executable
    assert "load_production_copy.py" in task.fixtures
    assert "load_production_copy.py" not in task.files
    assert task.checkpoint_policy.match("./migrate.sh")
    assert task.eval.tests.cmd == "python3 tests/test_billing.py --junitxml /tmp/sfork-junit.xml"
