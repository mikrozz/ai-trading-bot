#!/usr/bin/env bash
# Restore TimescaleDB from pg_dump -Fc. DESTRUCTIVE for target DB schema/data.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <dump_file> [--yes]" >&2
  exit 2
fi

DUMP="$1"
CONFIRM="${2:-}"
CONTAINER="${TIMESCALE_CONTAINER:-ai-trading-bot-timescaledb-1}"
DB_USER="${POSTGRES_USER:-trading}"
DB_NAME="${POSTGRES_DB:-trading}"

if [[ ! -f "$DUMP" ]]; then
  echo "RESTORE_FAIL dump not found: $DUMP" >&2
  exit 1
fi

if [[ "$CONFIRM" != "--yes" ]]; then
  echo "Это перезапишет БД $DB_NAME в контейнере $CONTAINER."
  echo "Для подтверждения запустите: $0 $DUMP --yes"
  exit 3
fi

if ! docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
  echo "RESTORE_FAIL container not running: $CONTAINER" >&2
  exit 1
fi

echo "RESTORE_START dump=$DUMP"
docker cp "$DUMP" "$CONTAINER:/tmp/trading_restore.dump"
# clean public schema objects (keep extensions where possible)
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" <<'SQL'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO public;
SQL
docker exec "$CONTAINER" pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner --role="$DB_USER" /tmp/trading_restore.dump
docker exec "$CONTAINER" rm -f /tmp/trading_restore.dump
echo "RESTORE_OK dump=$DUMP"
