#!/usr/bin/env bash
# One-time server preparation. Run as root on the server:
#
#   curl -fsSL https://raw.githubusercontent.com/rezashokrzad/persian-voice-assistant/main/scripts/server_setup.sh | bash
#
# Short to type (a console without paste):
#   git clone https://github.com/rezashokrzad/persian-voice-assistant /tmp/a
#   bash /tmp/a/scripts/server_setup.sh
#
# It is safe to run twice. It does NOT touch the firewall, the SSH settings, your existing nginx sites
# (except one added `include` line in the didrah.com HTTPS block, backed up and tested with `nginx -t`),
# or an existing Docker installation.
#
# Settings (environment variables):
#   GITHUB_USER=rezashokrzad     public SSH keys of this GitHub user are installed for `deploy`
#   DOMAIN=didrah.com            the site that gets the /dashboard location
#   REPO=https://github.com/rezashokrzad/persian-voice-assistant.git
set -euo pipefail

GITHUB_USER="${GITHUB_USER:-rezashokrzad}"
DOMAIN="${DOMAIN:-didrah.com}"
REPO="${REPO:-https://github.com/rezashokrzad/persian-voice-assistant.git}"
RAW="https://raw.githubusercontent.com/rezashokrzad/persian-voice-assistant/main"
SNIPPET=/etc/nginx/snippets/assistant-dashboard.conf

log() { printf '\n\033[1m[setup]\033[0m %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash ...)" >&2; exit 1; }
. /etc/os-release
[ "$ID" = "ubuntu" ] || { echo "this script is written for Ubuntu (found: $ID)" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

log "1/6 base packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3 ssh-import-id >/dev/null

log "2/6 user 'deploy' (no sudo rights; it only needs Docker)"
if ! id deploy >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" deploy
fi
sudo -u deploy ssh-import-id "gh:$GITHUB_USER" || echo "WARNING: could not import the GitHub keys; add a key to /home/deploy/.ssh/authorized_keys by hand"

log "3/6 Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker and the Compose plugin are already installed: leaving them (and /etc/docker/daemon.json) alone."
else
  install -m 0755 -d /etc/apt/keyrings
  if curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; then
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  else
    echo "download.docker.com unreachable: using Ubuntu's own packages"
    apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
  fi
  # Containers must use the server's own resolvers: public ones (1.1.1.1, 8.8.8.8) are blocked on some networks.
  if [ -f "$(dirname "${BASH_SOURCE[0]:-$0}")/fix_docker_dns.sh" ]; then
    bash "$(dirname "${BASH_SOURCE[0]:-$0}")/fix_docker_dns.sh" || echo "WARNING: could not set Docker DNS; see docs/DEPLOY.md"
  else
    curl -fsSL "$RAW/scripts/fix_docker_dns.sh" | bash || echo "WARNING: could not set Docker DNS; see docs/DEPLOY.md"
  fi
fi
usermod -aG docker deploy
if sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
  echo "WARNING: sshd restricts who may log in:"; sshd -T | grep -iE '^(allowusers|allowgroups) '
  echo "         add the user deploy there, or deploy will be refused over SSH."
fi
docker --version && docker compose version

log "4/6 the code (public repository)"
if [ -d /home/deploy/assistant/.git ]; then
  sudo -u deploy git -C /home/deploy/assistant pull --ff-only || true
else
  sudo -u deploy git clone "$REPO" /home/deploy/assistant
fi
sudo -u deploy mkdir -p /home/deploy/assistant/secrets
chmod 700 /home/deploy/assistant/secrets

log "5/6 nginx: /dashboard location for $DOMAIN"
if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed here: skipping (use PROXY_MODE=caddy, see docs/DEPLOY.md)"
else
  mkdir -p /etc/nginx/snippets
  cat > "$SNIPPET" <<'EOF'
# Dashboard of the voice assistant, served under /dashboard (the app adds the prefix to its own links).
location = /dashboard {
    return 301 /dashboard/;
}
location /dashboard/ {
    proxy_pass http://127.0.0.1:8081/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
EOF
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$HERE" ] && [ -f "$HERE/nginx_include.py" ]; then
    cp "$HERE/nginx_include.py" /tmp/nginx_include.py
  else
    curl -fsSL "$RAW/scripts/nginx_include.py" -o /tmp/nginx_include.py
  fi
  BK=/var/backups/assistant-nginx
  set +e
  out="$(python3 /tmp/nginx_include.py --domain "$DOMAIN" --include "snippets/assistant-dashboard.conf" --backup-dir "$BK")"
  rc=$?
  set -e
  echo "$out"
  edited="$(echo "$out" | sed -n 's/^added: //p')"
  if [ $rc -eq 0 ]; then
    if nginx -t; then
      systemctl reload nginx
      echo "nginx reloaded."
    else
      echo "nginx -t FAILED: restoring the backed-up file"
      [ -n "$edited" ] && cp -f "$BK/$(basename "$edited").bak" "$edited"
      nginx -t && systemctl reload nginx
      exit 1
    fi
  else
    echo "Could not edit nginx automatically (code $rc). Add this line inside the HTTPS server block of $DOMAIN, then run: nginx -t && systemctl reload nginx"
    echo "    include snippets/assistant-dashboard.conf;"
  fi
fi

log "6/6 what is already using the ports the assistant needs"
for p in 8000 8081 8765; do
  if ss -tln | awk '{print $4}' | grep -qE "[:.]$p$"; then echo "port $p is IN USE (pick another: API_PORT / DASHBOARD_PORT / MCP_PORT in .env)"; else echo "port $p is free"; fi
done

cat <<'EOF'

Done. Next, from YOUR computer (PowerShell), upload the two secret files:

  scp -i $env:USERPROFILE\.ssh\id_ed25519 secrets\server.env deploy@193.151.140.165:/home/deploy/assistant/.env
  scp -i $env:USERPROFILE\.ssh\id_ed25519 secrets\credentials.json deploy@193.151.140.165:/home/deploy/assistant/secrets/credentials.json

Then on the server:  ssh -i $env:USERPROFILE\.ssh\id_ed25519 deploy@193.151.140.165
  chmod 600 ~/assistant/.env ~/assistant/secrets/credentials.json
  cd ~/assistant && scripts/deploy.sh --no-pull
EOF
