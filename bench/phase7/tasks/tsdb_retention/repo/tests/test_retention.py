import datetime as dt
import os
import shutil
import subprocess

BLOCKS = "/var/lib/tsdb/blocks"


def days_on_disk():
    return sorted(dt.date.fromisoformat(f[:-4]) for f in os.listdir(BLOCKS) if f.endswith(".tsb"))


def test_store_verifies():
    r = subprocess.run(["tsdbctl", "verify"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_last_30_days_kept():
    today = dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 (sandbox python may predate dt.UTC)
    have = set(days_on_disk())
    missing = [d for d in (today - dt.timedelta(days=i) for i in range(30)) if d not in have]
    assert not missing, f"{len(missing)} of the last 30 days are missing, e.g. {missing[:3]}"


def test_older_blocks_dropped():
    today = dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 (sandbox python may predate dt.UTC)
    old = [d for d in days_on_disk() if d < today - dt.timedelta(days=30)]
    assert not old, f"{len(old)} blocks older than 30 days are still there, e.g. {old[:3]}"


def test_disk_has_room():
    assert shutil.disk_usage("/").free > 2 * 1024**3
