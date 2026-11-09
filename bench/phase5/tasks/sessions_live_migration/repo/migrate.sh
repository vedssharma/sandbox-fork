#!/bin/sh
# Apply a migration to the live session store: ./migrate.sh <name>
set -e
name="${1:?usage: ./migrate.sh <migration name, e.g. 0004_int_user_ids>}"
curl -sS --fail-with-body -X POST http://127.0.0.1:8080/admin/migrate -d "{\"name\": \"$name\"}"
echo
