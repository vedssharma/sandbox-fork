"""Runs inside the sandbox: decide whether SWE-bench FAIL_TO_PASS + PASS_TO_PASS tests pass.

    python3 swe_check.py SPEC.json

SPEC: {"runner": "pytest" | "django" | "sympy", "tests": [...], "test_files": [...]}. The current
directory is the repo (with the test patch applied). Exit 0 = every listed test passed.

- pytest: the ids are pytest node ids; pytest's exit code decides.
- django: `tests/runtests.py -v 2` over the modules of the test files; a test's name is the text
  before " ... <status>" on its result line (Django prints the docstring's first line there when the
  test has one), which is how SWE-bench names Django tests.
- sympy: `bin/test --verbose` over the test files; result lines are "test_name ok|F|E|f|s".

Missing tests count as failures, like in SWE-bench.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys


def run(cmd: list[str], timeout: int = 1500) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.stdout


def django(spec: dict) -> dict[str, str]:
    labels: list[str] = []
    for f in spec["test_files"]:
        if not (f.startswith("tests/") and f.endswith(".py")):
            continue
        parts = f[len("tests/") : -3].split("/")
        # a test module runs by itself; a changed models.py etc. means running its whole app
        label = ".".join(parts) if parts[-1].startswith("test") else parts[0]
        if label not in labels:
            labels.append(label)
    out = run([sys.executable, "tests/runtests.py", "--parallel", "1", "-v", "2", *labels])
    sys.stdout.write(out[-3000:])
    status: dict[str, str] = {}
    lines = out.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(.*?) \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$", line)
        if not m:
            continue
        name, st = m.group(1).strip(), m.group(2)
        status[name] = st
        # Python 3.11+ prints "test_x (mod.Class.test_x)"; SWE-bench ids for older Djangos are
        # "test_x (mod.Class)", so record both spellings
        m2 = re.match(r"^(\w+) \(([\w.]+)\.\1\)$", name)
        if m2:
            status.setdefault(f"{m2.group(1)} ({m2.group(2)})", st)
        # "test_x (mod.Class)\nDocstring ... ok": credit the test id line above as well
        prev = lines[i - 1].strip() if i else ""
        if re.match(r"^\w+ \([\w.]+\)$", prev):
            status.setdefault(prev, st)
            m2 = re.match(r"^(\w+) \(([\w.]+)\.\1\)$", prev)
            if m2:
                status.setdefault(f"{m2.group(1)} ({m2.group(2)})", st)
    return status


def sympy(spec: dict) -> dict[str, str]:
    out = run([sys.executable, "bin/test", "-C", "--verbose", *spec["test_files"]])
    sys.stdout.write(out[-3000:])
    status: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(test_\w+)\s+(ok|F|E|f|s|X|w)\b", line.strip())
        if m:
            status[m.group(1)] = {"ok": "ok", "f": "expected failure", "s": "skipped"}.get(
                m.group(2), m.group(2)
            )
    return status


def main() -> int:
    spec = json.load(open(sys.argv[1]))
    if spec["runner"] == "pytest":
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *spec["tests"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1500,
        )
        sys.stdout.write("\n".join(p.stdout.splitlines()[-25:]) + "\n")
        return p.returncode
    status = django(spec) if spec["runner"] == "django" else sympy(spec)
    bad = []
    for t in spec["tests"]:
        st = status.get(t)
        if st is None or not (st == "ok" or st.startswith("skipped") or st == "expected failure"):
            bad.append(f"{t}: {st or 'missing'}")
    print(json.dumps({"checked": len(spec["tests"]), "failed": bad[:20], "n_failed": len(bad)}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
