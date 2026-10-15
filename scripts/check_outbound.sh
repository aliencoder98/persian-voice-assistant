#!/usr/bin/env bash
# Run this on the server BEFORE installing anything: can it reach the services the assistant needs?
# Honors HTTPS_PROXY. Any HTTP answer (even 401/404) counts as reachable; only timeouts/resets fail.
#   bash scripts/check_outbound.sh
set -u

hosts=(
  "Telegram Bot API|https://api.telegram.org/"
  "DeepSeek API|https://api.deepseek.com/"
  "Resend API|https://api.resend.com/"
  "Google OAuth|https://oauth2.googleapis.com/token"
  "Google Calendar API|https://www.googleapis.com/calendar/v3/"
  "Docker registry|https://registry-1.docker.io/v2/"
  "Docker apt repo|https://download.docker.com/linux/ubuntu/"
  "Vosk model download|https://alphacephei.com/vosk/models/"
  "PyPI|https://pypi.org/"
  "Let's Encrypt|https://acme-v02.api.letsencrypt.org/directory"
)

fail=0
for item in "${hosts[@]}"; do
  name="${item%%|*}"; url="${item##*|}"
  out="$(curl -sS -o /dev/null -m 10 -w '%{http_code} %{time_total}s' "$url" 2>&1)"; rc=$?
  if [ $rc -eq 0 ] && [ "${out%% *}" != "000" ]; then
    printf '[ OK ] %-22s %s\n' "$name" "$out"
  else
    printf '[FAIL] %-22s %s\n' "$name" "$(echo "$out" | tail -1 | cut -c1-80)"
    fail=$((fail + 1))
  fi
done
echo
if [ $fail -eq 0 ]; then echo "Everything is reachable."; else echo "$fail unreachable: see docs/DEPLOY.md, 'Outbound access'."; fi
exit $((fail > 0))
