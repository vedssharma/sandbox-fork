#!/usr/bin/env bash
# Apply pending migrations in order, like the deploy pipeline does. Each file runs as its own psql
# script (statements autocommit), and its version is recorded in schema_migrations once it succeeds.
set -euo pipefail
cd "$(dirname "$0")"
export PGOPTIONS="-c client_min_messages=warning"
psql -qX -v ON_ERROR_STOP=1 -c "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
for f in migrations/*.sql; do
  v=$(basename "$f" | cut -d_ -f1)
  if [ -n "$(psql -qAtX -c "SELECT 1 FROM schema_migrations WHERE version = '$v'")" ]; then
    continue
  fi
  echo "applying $f"
  psql -qX -v ON_ERROR_STOP=1 -f "$f"
  psql -qX -v ON_ERROR_STOP=1 -c "INSERT INTO schema_migrations (version) VALUES ('$v')"
done
echo "schema at $(psql -qAtX -c 'SELECT max(version) FROM schema_migrations')"
