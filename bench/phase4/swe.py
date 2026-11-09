"""SWE-bench instances as sfork tasks (PLAN.md Phase 4; Phase 5 adds harder SWE-bench Verified ones).

`make_task(instance)` writes a task directory (task.md + sandbox-fork.yaml) that clones the repo at
the instance's base commit and installs pinned dependencies. The agent sees only the issue. What it
can check with `run_tests` (and what branch selection uses) is the repo's existing tests in the files
the hidden tests live in. The instance's hidden tests (FAIL_TO_PASS + PASS_TO_PASS, with the test
patch applied) never go onto the machine before the run ends: `hidden_check(instance)` is a shell
command the benchmark runs on the final head as a post-check, and the instance counts as solved only
if it exits 0.

Instances are pinned in `bench/*/swe/instances.json` (rows of princeton-nlp/SWE-bench_Lite or
SWE-bench_Verified). Supported repos: flask, django, sympy, pytest, pylint (see PROFILES).
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
BENCH = HERE.parent
INSTANCES = HERE / "swe" / "instances.json"
CHECKER = HERE / "swe_check.py"

# Dependency pins per (repo, version), chosen to match the release the base commit belongs to.
PINS: dict[tuple[str, str], str] = {
    ("pallets/flask", "2.0"): (
        "werkzeug==2.0.3 jinja2==3.0.3 itsdangerous==2.0.1 click==8.0.4 markupsafe==2.1.1 pytest==7.4.4"
    ),
    ("pallets/flask", "2.3"): (
        "werkzeug==2.3.3 jinja2==3.1.2 itsdangerous==2.1.2 click==8.1.3 blinker==1.6.2 "
        "markupsafe==2.1.2 pytest==7.4.4"
    ),
}

# How each repo is installed, imported, and tested. `runner` is the swe_check.py mode.
PROFILES: dict[str, dict[str, Any]] = {
    "pallets/flask": {"runner": "pytest", "import": "flask", "src": ["src/**"], "editable": "--no-deps -e ."},
    "django/django": {
        "runner": "django",
        "import": "django",
        "src": ["django/**"],
        "editable": "-e .",
        "deps": "",
    },
    "sympy/sympy": {
        "runner": "sympy",
        "import": "sympy",
        "src": ["sympy/**"],
        "editable": "-e .",
        "deps": "mpmath==1.3.0",
    },
    "pytest-dev/pytest": {
        "runner": "pytest",
        "import": "_pytest",
        "src": ["src/**"],
        "editable": "-e .",
        "deps": "pluggy==1.0.0 hypothesis==6.88.1 xmlschema==2.5.0 'setuptools<70' setuptools_scm==7.1.0",
    },
    "pylint-dev/pylint": {
        "runner": "pytest",
        "import": "pylint",
        "src": ["pylint/**"],
        "editable": "-e .",
        "deps": "pytest==7.4.4 'setuptools<70'",
    },
}
PROTECTED = {
    "django/django": ["tests/**"],
    "sympy/sympy": ["**/tests/**"],
    "pytest-dev/pytest": ["testing/**"],
}

TASK_MD = """\
Resolve this GitHub issue in the `{repo}` repository, which is checked out in /workspace at the \
commit the issue was filed against (the package is installed in editable mode).

Change the library source so the issue is resolved, in the way a maintainer would accept. Hidden \
tests written for the fix will be run after you finish, together with the existing test suite. \
Don't edit or add tests.

<issue>
{problem}
</issue>
"""

# Visible tests for runners that don't write junit: run them and print {"passed", "total"} last.
VISIBLE = r"""
import json, re, subprocess, sys
runner, files = sys.argv[1], sys.argv[2:]
if runner == "django":
    labels = []
    for f in files:
        parts = f[6:-3].split("/")
        lab = ".".join(parts) if parts[-1].startswith("test") else parts[0]
        if lab not in labels:
            labels.append(lab)
    cmd = [sys.executable, "tests/runtests.py", "--parallel", "1", "-v", "1", *labels]
else:
    cmd = [sys.executable, "bin/test", "-C", *files]
p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1500)
out = p.stdout
print(out[-6000:])
if runner == "django":
    ran = re.search(r"Ran (\d+) tests?", out)
    total = int(ran.group(1)) if ran else 0
    bad = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", out.split("Ran ")[-1]))
else:
    m = re.search(r"tests finished: (\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?(?:, (\d+) expected to fail)?(?:, (\d+) expected to fail, but passed)?(?:, (\d+) exceptions?)?", out)
    if m:
        passed = int(m.group(1)); failed = int(m.group(2) or 0); exc = int(m.group(6) or 0)
        total, bad = passed + failed + exc, failed + exc
    else:
        total, bad = 0, 0
print(json.dumps({"passed": total - bad if total else 0, "total": total or 1}))
"""


def load_instances() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(BENCH.glob("*/swe/instances.json")):
        for r in json.loads(f.read_text()):
            out[r["instance_id"]] = r
    return out


def _test_files(patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", patch, re.M)))


def _tests_list(inst: dict[str, Any]) -> list[str]:
    """FAIL_TO_PASS + PASS_TO_PASS ids. For pytest, parametrized ids are cut back to the test function
    (SWE-bench truncates some at spaces, so pytest can't find them); that runs every parameter."""
    runner = PROFILES[inst["repo"]]["runner"]
    out: list[str] = []
    for t in json.loads(inst["FAIL_TO_PASS"]) + json.loads(inst["PASS_TO_PASS"]):
        if runner == "pytest":
            t = t.split("[", 1)[0]
        if t not in out:
            out.append(t)
    return out


def visible_test_files(inst: dict[str, Any]) -> list[str]:
    """Existing test files the hidden tests live in (as of the base commit)."""
    if PROFILES[inst["repo"]]["runner"] == "pytest":
        return sorted({t.split("::", 1)[0] for t in _tests_list(inst)})
    # django / sympy ids don't carry a path: use the test patch's files that exist before the patch
    return [f for f in _test_files(inst["test_patch"]) if f"--- a/{f}" in inst["test_patch"]]


def python_for(inst: dict[str, Any]) -> str | None:
    """The Python an instance needs if the image's 3.11 won't do (None = use 3.11)."""
    major_minor = tuple(int(x) for x in re.findall(r"\d+", inst["version"])[:2])
    old = {
        "django/django": (4, 0),
        "sympy/sympy": (1, 5),
        "pytest-dev/pytest": (6, 0),
        "pylint-dev/pylint": (2, 13),
    }.get(inst["repo"])
    return "3.9" if old and major_minor < old else None


def _setup(inst: dict[str, Any]) -> list[str]:
    prof = PROFILES[inst["repo"]]
    url = f"https://github.com/{inst['repo']}"
    pins = PINS.get((inst["repo"], inst["version"]), prof.get("deps", ""))
    if inst["repo"] == "pytest-dev/pytest" and python_for(inst):
        pins = "attrs==19.3.0 more-itertools==8.14.0 'pluggy<1.0' py==1.11.0 wcwidth packaging hypothesis==6.88.1"
    steps = []
    if (py := python_for(inst)) is not None:
        # older releases don't run on the image's Python 3.11: put a uv-managed Python first on PATH
        steps.append(
            "pip install -q --disable-pip-version-check uv >/tmp/uv.log 2>&1 && "
            f"uv python install -q {py} >>/tmp/uv.log 2>&1 && uv venv -q --seed -p {py} /opt/py >>/tmp/uv.log 2>&1 && "
            # wrappers, not symlinks: a symlinked venv python wouldn't find its pyvenv.cfg
            "for b in python python3 pip pip3; do rm -f /usr/local/bin/$b; "
            "printf '#!/bin/sh\\nexec /opt/py/bin/%s \"$@\"\\n' $b > /usr/local/bin/$b; chmod +x /usr/local/bin/$b; done && "
            "python3 --version || (tail -20 /tmp/uv.log; exit 1)"
        )
    steps += [
        # seed_task already ran `git init` in the empty workdir; replace it with the real repo
        f"rm -rf /workspace/.git && git clone -q {url} /tmp/src && cd /tmp/src && "
        f"git checkout -q {inst['base_commit']} && cp -a /tmp/src/. /workspace/ && rm -rf /tmp/src",
        "git -c advice.detachedHead=false checkout -q -B main && git tag -f sfork-base >/dev/null",
    ]
    if pins:
        steps.append(
            f"pip install -q --disable-pip-version-check {pins} >/tmp/pip.log 2>&1 || (tail -20 /tmp/pip.log; exit 1)"
        )
    steps.append(
        f"pip install -q --disable-pip-version-check {prof['editable']} >>/tmp/pip.log 2>&1 "
        "|| (tail -20 /tmp/pip.log; exit 1)"
    )
    if prof["runner"] != "pytest":
        b64 = base64.b64encode(VISIBLE.encode()).decode()
        steps.append(f"mkdir -p /opt/sfork && echo {b64} | base64 -d > /opt/sfork/visible_tests.py")
    return steps


def eval_config(inst: dict[str, Any]) -> dict[str, Any]:
    prof = PROFILES[inst["repo"]]
    files = visible_test_files(inst)
    if prof["runner"] == "pytest":
        tests: dict[str, Any] = {
            "cmd": "python3 -m pytest -q -p no:cacheprovider "
            + " ".join(files)
            + " --junitxml /tmp/sfork-junit.xml",
            "report": "junit:/tmp/sfork-junit.xml",
        }
    else:
        tests = {
            "cmd": f"python3 /opt/sfork/visible_tests.py {prof['runner']} " + " ".join(files),
            "parse": "json",
        }
    flask = inst["repo"] == "pallets/flask"  # Phase 4's settings, kept so its configs don't change
    tests.update(
        timeout=600 if flask else 1500,
        # A correct fix may legitimately break a visible test that the hidden test patch updates
        # (flask-4045 does), so visible regressions don't disqualify.
        regressions="ignore",
    )
    return {
        "workdir": "/workspace",
        "setup": _setup(inst),
        "gates": [
            {
                "name": "imports",
                "cmd": f"python3 -c 'import {prof['import']}'",
                "timeout": 60 if flask else 120,
            }
        ],
        "tests": tests,
        "quality": {"protected": PROTECTED.get(inst["repo"], ["tests/**"]), "expected": prof["src"]},
        # Visible tests don't decide correctness here, so they don't dominate the ranking.
        "correctness_first": False,
        "weights": {"tests": 0.5, "diff_size": 0.5},
        "judge": {"enabled": True, "margin": 0.15},
    }


def make_task(inst: dict[str, Any], out_dir: Path) -> Path:
    d = out_dir / inst["instance_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.md").write_text(TASK_MD.format(repo=inst["repo"], problem=inst["problem_statement"].strip()))
    (d / "sandbox-fork.yaml").write_text(
        "# Generated by bench/phase4/swe.py from SWE-bench; hidden tests are not in here.\n"
        + yaml.safe_dump(eval_config(inst), sort_keys=False, width=100)
    )
    return d


def hidden_check(inst: dict[str, Any], *, patch: str | None = None) -> str:
    """Shell command: in a copy of the workdir, restore the test files, apply the test patch (and
    `patch`, e.g. the gold patch for validation), and run FAIL_TO_PASS + PASS_TO_PASS. Exit 0 = solved."""
    prof = PROFILES[inst["repo"]]
    tp = inst["test_patch"]
    tfiles = _test_files(tp)
    files = " ".join(shlex.quote(f) for f in tfiles)
    b64 = base64.b64encode(tp.encode()).decode()
    gold = ""
    if patch is not None:
        gold = f"echo {base64.b64encode(patch.encode()).decode()} | base64 -d > /tmp/sfork-gold.patch && git apply /tmp/sfork-gold.patch && "
    if inst["repo"] == "pallets/flask":  # Phase 4's exact command, kept so its results stay comparable
        tests = " ".join(shlex.quote(t) for t in _tests_list(inst))
        run = (
            f"PYTHONPATH=/tmp/sfork-hidden/src python3 -m pytest -q -p no:cacheprovider -o addopts='' {tests} "
            "2>&1 | tail -n 25; rc=${PIPESTATUS[0]}"
        )
    else:
        spec = {"runner": prof["runner"], "tests": _tests_list(inst), "test_files": tfiles}
        spec_b64 = base64.b64encode(json.dumps(spec).encode()).decode()
        chk_b64 = base64.b64encode(CHECKER.read_bytes()).decode()
        src = "/tmp/sfork-hidden/src" if prof["src"] == ["src/**"] else "/tmp/sfork-hidden"
        run = (
            f"echo {spec_b64} | base64 -d > /tmp/sfork-spec.json && echo {chk_b64} | base64 -d > /tmp/sfork-check.py && "
            f"PYTHONPATH={src} python3 /tmp/sfork-check.py /tmp/sfork-spec.json 2>&1 | tail -n 40; rc=${{PIPESTATUS[0]}}"
        )
    return (
        "rm -rf /tmp/sfork-hidden && cp -a /workspace /tmp/sfork-hidden && cd /tmp/sfork-hidden && "
        f"git checkout -q sfork-base -- {files} 2>/dev/null; "
        f"{gold}echo {b64} | base64 -d > /tmp/sfork-test.patch && git apply /tmp/sfork-test.patch && "
        f"{run}; cd / && rm -rf /tmp/sfork-hidden; exit $rc"
    )


if __name__ == "__main__":
    import sys

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else INSTANCES
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "tasks"
    for inst in json.loads(src.read_text()):
        print(make_task(inst, out))
