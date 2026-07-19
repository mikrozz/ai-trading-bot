#!/usr/bin/env bash
# Backup TimescaleDB (trading) — pg_dump custom format + retention.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker-compose.yml}"
CONTAINER="${TIMESCALE_CONTAINER:-ai-trading-bot-timescaledb-1}"
DB_USER="${POSTGRES_USER:-trading}"
DB_NAME="${POSTGRES_DB:-trading}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/data/backups/timescaledb}"
KEEP_DAYS="${KEEP_DAYS:-7}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/trading_${STAMP}.dump"

mkdir -p "$BACKUP_DIR"

if ! docker inspect "$CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
  echo "BACKUP_FAIL container not running: $CONTAINER" >&2
  exit 1
fi

echo "BACKUP_START container=$CONTAINER out=$OUT"
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -f "/tmp/trading_backup.dump"
docker cp "$CONTAINER:/tmp/trading_backup.dump" "$OUT"
docker exec "$CONTAINER" rm -f /tmp/trading_backup.dump
chmod 600 "$OUT"

# prune old dumps
find "$BACKUP_DIR" -type f -name 'trading_*.dump' -mtime +"$KEEP_DAYS" -delete || true

SIZE="$(du -h "$OUT" | awk '{print $1}')"
echo "BACKUP_OK path=$OUT size=$SIZE keep_days=$KEEP_DAYS"
