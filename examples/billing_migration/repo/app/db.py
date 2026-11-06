"""Tiny Postgres helper over the psql CLI (stdlib only)."""

import subprocess


def query(sql: str, *params: object) -> list[list[str]]:
    """Run one SQL statement with psql variables :'p1', :'p2', ... and return rows of strings."""
    args = ["psql", "-qAtX", "-v", "ON_ERROR_STOP=1", "-F", "\t"]
    for i, p in enumerate(params, 1):
        args += ["-v", f"p{i}={p}"]
    out = subprocess.run(args, input=sql, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [line.split("\t") for line in out.stdout.splitlines() if line]
