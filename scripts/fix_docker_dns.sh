#!/usr/bin/env bash
# Make Docker containers use the server's own DNS resolvers (public ones such as 1.1.1.1 or 8.8.8.8
# are blocked on some networks, which breaks every container with "Temporary failure in name resolution").
# Run as root:   bash scripts/fix_docker_dns.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

src=/run/systemd/resolve/resolv.conf
[ -f "$src" ] || src=/etc/resolv.conf
servers="$(awk '/^nameserver/ {print $2}' "$src" | grep -vE '^(127\.|::1)' | head -3 | tr '\n' ' ')"
if [ -z "$servers" ]; then echo "no upstream resolver found in $src" >&2; exit 1; fi
echo "server resolvers: $servers"

mkdir -p /etc/docker
python3 - "$servers" <<'PY'
import json, os, sys
path = "/etc/docker/daemon.json"
cfg = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
cfg["dns"] = sys.argv[1].split()
cfg.setdefault("log-driver", "json-file")
cfg.setdefault("log-opts", {"max-size": "10m", "max-file": "5"})
json.dump(cfg, open(path, "w"), indent=2)
print(open(path).read())
PY
systemctl restart docker
sleep 3
echo "--- host resolves:"
for h in pypi.org files.pythonhosted.org registry-1.docker.io; do printf '%-28s' "$h"; getent hosts "$h" | head -1 | awk '{print $1}' || echo FAIL; done
echo "Done. Docker now uses: $servers"
