#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh.
#
#   scripts/restore.sh backups/assistant-XXXX.dump             # TEST: restore into a scratch database,
#                                                              #       print row counts, drop it. Touches nothing live.
#   scripts/restore.sh backups/assistant-XXXX.dump --replace   # REAL: replace the live database
#
# COMPOSE_FILE defaults to docker-compose.prod.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
MODE="${2:-test}"
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "usage: $0 <backup.dump> [--replace]" >&2; exit 1; }
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

if [ -f "$FILE.sha256" ]; then
  (cd "$(dirname "$FILE")" && sha256sum -c "$(basename "$FILE").sha256") || { echo "Checksum mismatch: the backup is damaged." >&2; exit 1; }
fi

COUNTS="SELECT 'users' AS tbl, count(*) FROM users UNION ALL SELECT 'contacts', count(*) FROM contacts
 UNION ALL SELECT 'voice_messages', count(*) FROM voice_messages UNION ALL SELECT 'agent_runs', count(*) FROM agent_runs
 UNION ALL SELECT 'tool_calls', count(*) FROM tool_calls UNION ALL SELECT 'llm_calls', count(*) FROM llm_calls
 UNION ALL SELECT 'audit_events', count(*) FROM audit_events UNION ALL SELECT 'emails', count(*) FROM emails
 UNION ALL SELECT 'calendar_events', count(*) FROM calendar_events UNION ALL SELECT 'memory_facts', count(*) FROM memory_facts
 UNION ALL SELECT 'alembic_version=' || version_num, 1 FROM alembic_version ORDER BY 1;"

if [ "$MODE" = "--replace" ]; then
  DB="$("${COMPOSE[@]}" exec -T db sh -c 'printf %s "$POSTGRES_DB"' </dev/null)"
  echo "This REPLACES the live database '$DB' with $FILE."
  echo "Everything written since that backup is lost. Type the database name to continue:"
  read -r answer
  [ "$answer" = "$DB" ] || { echo "Aborted."; exit 1; }

  echo "[restore] stopping the apps (the database keeps running)..."
  "${COMPOSE[@]}" stop bot api dashboard mcp 2>/dev/null || true
  echo "[restore] restoring..."
  "${COMPOSE[@]}" exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error' < "$FILE"
  echo "[restore] re-applying migrations and the dashboard role, then starting the apps..."
  "${COMPOSE[@]}" run --rm migrate
  "${COMPOSE[@]}" up -d
  echo "[restore] done. Row counts:"
  "${COMPOSE[@]}" exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<< "$COUNTS"
  exit 0
fi

SCRATCH="restore_test_$(date +%s)"
echo "[restore-test] creating scratch database $SCRATCH..."
"${COMPOSE[@]}" exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d postgres -qc 'CREATE DATABASE $SCRATCH'"
cleanup() { "${COMPOSE[@]}" exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d postgres -qc 'DROP DATABASE IF EXISTS $SCRATCH'" >/dev/null; }
trap cleanup EXIT
echo "[restore-test] restoring $FILE..."
"${COMPOSE[@]}" exec -T db sh -c "pg_restore -U \"\$POSTGRES_USER\" -d $SCRATCH --no-owner --exit-on-error" < "$FILE"
echo "[restore-test] row counts in the restored copy:"
"${COMPOSE[@]}" exec -T db sh -c "psql -U \"\$POSTGRES_USER\" -d $SCRATCH" <<< "$COUNTS"
echo "[restore-test] OK. Scratch database dropped; the live database was not touched."
