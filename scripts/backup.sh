#!/usr/bin/env bash
# PostgreSQL backup: dump -> verify -> keep the last N locally -> copy off the server.
#
#   scripts/backup.sh                    # dump and keep locally (warns that it is not off-server)
#   scripts/backup.sh --require-offsite  # also exit non-zero unless the off-server copy succeeded (use in cron)
#
# Settings come from the environment or scripts/backup.conf (plain shell, not in git):
#   COMPOSE_FILE=docker-compose.prod.yml   BACKUP_DIR=./backups   KEEP_LOCAL=7
#   BACKUP_RCLONE_REMOTE=myremote:assistant-backups     (needs rclone configured)   -- or --
#   BACKUP_SCP_TARGET=backup@other-host:/srv/backups/assistant   (needs SSH key login)
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f scripts/backup.conf ] && . scripts/backup.conf
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_LOCAL="${KEEP_LOCAL:-7}"
REQUIRE_OFFSITE=0
[ "${1:-}" = "--require-offsite" ] && REQUIRE_OFFSITE=1

COMPOSE=(docker compose -f "$COMPOSE_FILE")
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
file="$BACKUP_DIR/assistant-$ts.dump"
partial="$file.partial"

echo "[backup] dumping..."
# -Fc: compressed custom format. Credentials come from the db container's own environment.
"${COMPOSE[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$partial"

echo "[backup] verifying archive..."
if [ ! -s "$partial" ] || ! "${COMPOSE[@]}" exec -T db pg_restore --list < "$partial" > /dev/null; then
  rm -f "$partial"
  echo "[backup] FAILED: the dump is empty or unreadable" >&2
  exit 1
fi
mv "$partial" "$file"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
echo "[backup] wrote $file ($(du -h "$file" | cut -f1))"

# keep the newest KEEP_LOCAL dumps
ls -1t "$BACKUP_DIR"/assistant-*.dump 2>/dev/null | tail -n +"$((KEEP_LOCAL + 1))" | while read -r old; do
  rm -f "$old" "$old.sha256"
done

offsite=0
if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  if rclone copy "$file" "$file.sha256" "$BACKUP_RCLONE_REMOTE"; then offsite=1; echo "[backup] copied to $BACKUP_RCLONE_REMOTE"; fi
elif [ -n "${BACKUP_SCP_TARGET:-}" ]; then
  if scp -q -o BatchMode=yes "$file" "$file.sha256" "$BACKUP_SCP_TARGET/"; then offsite=1; echo "[backup] copied to $BACKUP_SCP_TARGET"; fi
fi

if [ "$offsite" -eq 0 ]; then
  echo "[backup] WARNING: no off-server copy (set BACKUP_RCLONE_REMOTE or BACKUP_SCP_TARGET). A backup on the same disk does not survive losing the server." >&2
  [ "$REQUIRE_OFFSITE" -eq 1 ] && exit 2
fi
exit 0
