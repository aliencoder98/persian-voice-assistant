#!/usr/bin/env bash
# Update the production stack. Safe to run twice: with nothing new it rebuilds from cache,
# changes nothing, and still checks health.
#
#   scripts/deploy.sh              # update: backup, git pull, build, migrate, restart, health check
#   scripts/deploy.sh rollback     # go back to the previously deployed commit
#   scripts/deploy.sh status       # what is deployed, container health
#   scripts/deploy.sh --no-pull    # deploy the working tree as it is (first install, or no git remote)
#
# Run it from the project directory as the deploy user (not root).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
STATE_DIR=".deploy"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
SERVICES_TO_CHECK=(db bot api dashboard)
mkdir -p "$STATE_DIR"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# one deploy at a time (mkdir is atomic and works everywhere; a crash can leave a stale lock)
LOCK="$STATE_DIR/lock.d"
mkdir "$LOCK" 2>/dev/null || die "another deploy is running (if none is, remove the stale lock: rmdir $LOCK)"
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

[ -f .env ] || die ".env is missing (copy .env.example and fill it in; see docs/DEPLOY.md)"

if grep -q 'CHANGE_ME' .env; then
  die ".env still contains CHANGE_ME placeholders: $(grep -o '^[A-Z_]*=CHANGE_ME[^ ]*' .env | cut -d= -f1 | tr '
' ' ')"
fi

# PROXY_MODE=caddy (default): Caddy serves the dashboard on 80/443 with automatic HTTPS.
# PROXY_MODE=nginx: your existing nginx already owns 80/443 and forwards to 127.0.0.1:${DASHBOARD_PORT}.
PROXY_MODE="$( { grep -E '^PROXY_MODE=' .env || true; } | tail -1 | cut -d= -f2- | tr -d '\015"' | tr -d "'" )"
PROXY_MODE="${PROXY_MODE:-caddy}"
case "$PROXY_MODE" in
  caddy) export COMPOSE_PROFILES=caddy; SERVICES_TO_CHECK+=(proxy) ;;
  nginx) unset COMPOSE_PROFILES ;;
  *) die "PROXY_MODE must be caddy or nginx (got: $PROXY_MODE)" ;;
esac
log "proxy mode: $PROXY_MODE"
export APP_TAG   # set below; compose uses it to name the image per commit

wait_healthy() {
  log "waiting for services to become healthy (up to ${HEALTH_TIMEOUT}s)..."
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) bad
  while :; do
    bad=""
    for svc in "${SERVICES_TO_CHECK[@]}"; do
      local id state
      id="$(compose ps -q "$svc" 2>/dev/null || true)"
      state="$([ -n "$id" ] && docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)"
      [ "$state" = "healthy" ] || [ "$state" = "running" ] || bad="$bad $svc=$state"
    done
    [ -z "$bad" ] && { log "all healthy"; return 0; }
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "NOT healthy:$bad"
      compose ps || true
      for svc in "${SERVICES_TO_CHECK[@]}"; do compose logs --tail 15 "$svc" 2>/dev/null || true; done
      return 1
    fi
    sleep 5
  done
}

bring_up() {
  local sha="$1"
  APP_TAG="$sha"
  log "building image persian-assistant:$APP_TAG..."
  compose build
  log "applying migrations and starting services..."
  compose up -d --remove-orphans
  wait_healthy
}

record() {  # remember what was running before, only when it changes
  local new="$1" cur=""
  [ -f "$STATE_DIR/current" ] && cur="$(cat "$STATE_DIR/current")"
  if [ "$cur" != "$new" ]; then
    [ -n "$cur" ] && echo "$cur" > "$STATE_DIR/previous"
    echo "$new" > "$STATE_DIR/current"
  fi
}

cmd="${1:-update}"
case "$cmd" in
  status)
    log "deployed commit: $(cat "$STATE_DIR/current" 2>/dev/null || echo unknown); previous: $(cat "$STATE_DIR/previous" 2>/dev/null || echo none)"
    log "checked out:     $(git rev-parse --short HEAD)"
    compose ps
    ;;

  update|--no-pull)
    [ -z "$(git status --porcelain --untracked-files=no)" ] || die "tracked files were modified on the server; commit or revert them first"
    if [ "$cmd" = "update" ]; then
      if git remote | grep -q .; then
        log "git pull --ff-only"
        git pull --ff-only
      else
        log "no git remote configured: deploying the working tree as it is"
      fi
    fi
    sha="$(git rev-parse --short HEAD)"
    log "target commit: $sha"

    if compose ps -q db 2>/dev/null | grep -q .; then
      log "pre-deploy backup..."
      scripts/backup.sh || log "WARNING: backup failed (continuing; check scripts/backup.sh)"
    fi

    if bring_up "$sha"; then
      record "$sha"
      log "deployed $sha"
    else
      log "the new version did not become healthy."
      if [ -f "$STATE_DIR/current" ] && [ "$(cat "$STATE_DIR/current")" != "$sha" ]; then
        log "roll back with:  scripts/deploy.sh rollback   (previous good commit: $(cat "$STATE_DIR/current"))"
      fi
      exit 1
    fi
    ;;

  rollback)
    [ -f "$STATE_DIR/current" ] || die "nothing recorded to roll back to"
    [ -f "$STATE_DIR/previous" ] || die "no previous deployment is recorded"
    target="$(cat "$STATE_DIR/previous")"
    now="$(cat "$STATE_DIR/current")"
    log "rolling back $now -> $target"
    git checkout --detach "$target"
    if bring_up "$target"; then
      echo "$now" > "$STATE_DIR/previous"
      echo "$target" > "$STATE_DIR/current"
      log "rolled back to $target. The code is a detached checkout: 'git checkout main && scripts/deploy.sh' returns to the latest."
      log "If the newer version ran a database migration, see docs/DEPLOY.md ('Rollback and migrations')."
    else
      die "rollback did not become healthy; see the logs above"
    fi
    ;;

  *)
    die "unknown command: $cmd (use update, rollback, status or --no-pull)"
    ;;
esac
