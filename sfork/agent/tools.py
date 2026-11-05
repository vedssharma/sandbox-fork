"""Tools the agent drives its sandbox with (PLAN.md section 5.2).

`fork` and `done` are control tools: the agent loop hands them to the orchestrator instead of
running them in the sandbox. checkpoint/rewind tools come later; the engine already supports them.
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
DONE = _tool(
    "done",
    "Finish: your work is ready to be evaluated. Call it once, after run_tests looks as good as you can "
    "make it.",
    {"summary": {"type": "string", "description": "what you changed and how it scored"}},
)

ALL_TOOLS = [BASH, READ_FILE, WRITE_FILE, RUN_TESTS, FORK, DONE]
CONTROL_TOOLS = {"fork", "done"}
