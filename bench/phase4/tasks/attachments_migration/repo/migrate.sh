#!/usr/bin/env bash
# Apply pending migrations in order, like the deploy pipeline does. Each migrations/NNNN_*.py runs
# once; its version is recorded in schema_migrations after it succeeds.
set -euo pipefail
cd "$(dirname "$0")"
db() { python3 -c "import sqlite3,sys; c=sqlite3.connect('/srv/app/app.db'); r=c.execute(sys.argv[1]).fetchall(); c.commit(); print('\n'.join('|'.join(map(str,x)) for x in r))" "$1"; }
for f in migrations/*.py; do
  v=$(basename "$f" | cut -d_ -f1)
  if [ -n "$(db "SELECT 1 FROM schema_migrations WHERE version = '$v'")" ]; then
    continue
  fi
  echo "applying $f"
  python3 "$f"
  db "INSERT INTO schema_migrations (version) VALUES ('$v')" >/dev/null
done
echo "schema at $(db 'SELECT max(version) FROM schema_migrations')"
