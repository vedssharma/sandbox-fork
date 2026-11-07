# Reference fix used only by bench/phase4/validate.py: keep names unique per owner, then migrate.
python3 - <<'PY'
p = "migrations/0003_shard_uploads.py"
s = open(p).read()
s = s.replace('dest = os.path.join(folder, filename.lower())',
              'dest = os.path.join(folder, f"{attachment_id}-{filename.lower()}")')
open(p, "w").write(s)
PY
./migrate.sh
