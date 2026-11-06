"""Tools the agent drives its sandbox with (PLAN.md section 5.2).

`fork`, `checkpoint`, `rewind` and `done` are control tools: the agent loop hands them to the
orchestrator instead of running them in the sandbox.
"""

from __future__ import annotations

from typing import Any


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


BASH = _tool(
    "bash",
    "Run a shell command in your sandbox, from the project directory. Returns exit code and combined "
    "stdout/stderr (long output is truncated). Each call is a fresh shell; use files for state.",
    {"command": {"type": "string"}},
)
READ_FILE = _tool(
    "read_file",
    "Read a text file. Relative paths are resolved from the project directory.",
    {"path": {"type": "string"}},
)
WRITE_FILE = _tool(
    "write_file",
    "Create or overwrite a text file with the given full contents. Relative paths are resolved from "
    "the project directory.",
    {"path": {"type": "string"}, "content": {"type": "string"}},
)
RUN_TESTS = _tool(
    "run_tests",
    "Run the project's test suite and benchmarks exactly as the evaluator will, and return the "
    "results. Use it to check your work before calling done.",
    {},
)
FORK = _tool(
    "fork",
    "Split into parallel branches when there are 2-4 materially different approaches and you can't "
    "confidently say which will score best. The whole machine (files, installed packages, running "
    "processes) and this conversation are copied once per alternative; each copy pursues one "
    "alternative. An evaluator then runs the tests and benchmarks in every branch, keeps the best "
    "one and deletes the rest. Don't fork over trivia (naming, formatting) or when one approach is "
    "clearly right. Each plan should say concretely what that branch will do.",
    {
        "alternatives": {
            "type": "array",
            "description": "2 to 4 alternatives",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short label, e.g. 'sieve'"},
                    "plan": {"type": "string", "description": "what this branch will do"},
                },
                "required": ["name", "plan"],
                "additionalProperties": False,
            },
        }
    },
)
CHECKPOINT = _tool(
    "checkpoint",
    "Save a restore point of the whole machine: files, databases, installed packages and running "
    "processes, plus this conversation. Returns a checkpoint id for rewind. Commands that look "
    "risky (migrations, DROP/TRUNCATE, rm -rf, ...) are checkpointed automatically before they run, "
    "so you rarely need this yourself; use it before other steps that are hard to undo.",
    {"label": {"type": "string", "description": "what state this is, e.g. 'before backfill'"}},
)
REWIND = _tool(
    "rewind",
    "Go back in time to a checkpoint: the machine returns exactly to its state at that checkpoint "
    "(files, databases, processes) and this conversation is cut back to that point, so everything "
    "after it is undone. Use it when a step damaged state you can't cleanly repair (lost data, a "
    "half-applied migration, a broken environment). You keep only the lesson you write here, so "
    "make it say what went wrong and what to do differently. Any other tool calls in the same turn "
    "are not run.",
    {
        "checkpoint_id": {"type": "string"},
        "lesson": {"type": "string", "description": "what went wrong and what to do instead"},
    },
)
DONE = _tool(
    "done",
    "Finish: your work is ready to be evaluated. Call it once, after run_tests looks as good as you can "
    "make it.",
    {"summary": {"type": "string", "description": "what you changed and how it scored"}},
)

ALL_TOOLS = [BASH, READ_FILE, WRITE_FILE, RUN_TESTS, FORK, CHECKPOINT, REWIND, DONE]
TOOLS_WITHOUT_REWIND = [t for t in ALL_TOOLS if t["name"] not in ("checkpoint", "rewind")]
CONTROL_TOOLS = {"fork", "checkpoint", "rewind", "done"}
