"""Setup only (deleted afterwards): install tinykv 1.4.2, build the 2.0.0 wheel into /opt/wheels,
and load the 'production' note store with tinykv 1.x, recording checksums for the tests."""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys

FIX = os.path.dirname(os.path.abspath(__file__))
pip = [sys.executable, "-m", "pip", "-q", "--disable-pip-version-check"]
subprocess.run([*pip, "install", f"{FIX}/tinykv-1"], check=True)
os.makedirs("/opt/wheels", exist_ok=True)
subprocess.run([*pip, "wheel", "--no-deps", "-w", "/opt/wheels", f"{FIX}/tinykv-2"], check=True)
shutil.copy(f"{FIX}/tinykv-2/CHANGELOG.md", "/opt/wheels/tinykv-2.0.0-CHANGELOG.md")
# 1.4.2 isn't kept anywhere once it's uninstalled, like a yanked release.
subprocess.run(["rm", "-rf", os.path.expanduser("~/.cache/pip")], check=False)

import tinykv  # noqa: E402  (1.4.2, just installed)

rng = random.Random()
words = (
    "café crème brûlée naïve façade Zürich Mötley señor jalapeño piñata São Paulo Ærø résumé "
    "déjà vu fiancée Ångström Øresund garçon über Straße coördinate plain ascii notes meeting todo "
    "budget review deploy rollback"
).split()
os.makedirs("/var/lib/notes", exist_ok=True)
store = tinykv.connect("/var/lib/notes/store")
expected = {}
for i in range(240):
    title = (
        f"note-{i:03d}-" + rng.choice(words).encode("ascii", "ignore").decode() + f"-{rng.randint(0, 99999)}"
    )
    body = " ".join(rng.choice(words) for _ in range(rng.randint(5, 40)))
    store.put(title, body)
    expected[title] = hashlib.sha256(body.encode("utf-8")).hexdigest()
json.dump({"notes": expected}, open("/var/lib/notes/expected.json", "w"))
print(f"tinykv {tinykv.__version__}: loaded {len(expected)} notes")
