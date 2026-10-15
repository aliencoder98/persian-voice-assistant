# Deploying to your Linux server (cloud.ir)

Everything here is run **by you** over SSH. Nothing in this repository connects to your server.
Follow the sections in order; each ends with a check so you know it worked before moving on.

**What runs where**

| Piece | Exposure |
|---|---|
| Telegram bot | long polling: **no inbound port** |
| API (`/health`, admin endpoints) | `127.0.0.1:8000` only (reach it with an SSH tunnel) |
| Dashboard | HTTPS via Caddy on `https://usage.example.com` (ports 80/443) |
| PostgreSQL | not published at all; only other containers reach it |
| MCP server | off by default (optional profile, `127.0.0.1` only) |

**Read this first if the server is inside Iran.** Telegram, Google, Docker Hub and sometimes other hosts can be blocked or filtered there. Section 3 checks this *before* you install anything, and gives the fixes (proxy, API gateway, offline image loading). Do not skip it.

## 0. What you need before you start

| Item | Where it comes from |
|---|---|
| A server (Ubuntu 22.04 or 24.04, 2 vCPU, 2 GB RAM, 20 GB disk is enough) and its public IP | cloud.ir panel |
| A domain you control, and a name such as `usage.example.com` for the dashboard | your DNS provider |
| `TELEGRAM_BOT_TOKEN`, your numeric `ALLOWED_USER_IDS` | @BotFather, @userinfobot |
| `DEEPSEEK_API_KEY` | DeepSeek platform |
| `RESEND_API_KEY`, a sender address on a verified domain for `EMAIL_FROM` | Resend |
| `secrets/credentials.json` (Google OAuth "Desktop app" client) and `GOOGLE_REFRESH_TOKEN` | README, section "Google Calendar" (the token is made on **your computer**) |
| Somewhere else to keep backups (another server, or an S3-compatible bucket) | your choice |
| A private Git remote (GitHub/GitLab), or use the `git bundle` fallback in 4.2 | your choice |

In the cloud.ir panel also open inbound **22, 80 and 443** in the server's firewall / security group.

## 1. Prepare the server

Log in once as root (or the initial user cloud.ir gave you) and prepare a non-root `deploy` user.

```bash
apt update && apt -y upgrade
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
passwd deploy                      # set a password: sudo asks for it
```

### 1.1 SSH key login

On **your computer** (skip `ssh-keygen` if you already have a key):

```bash
ssh-keygen -t ed25519 -C "assistant-deploy"
ssh-copy-id deploy@YOUR_SERVER_IP
ssh deploy@YOUR_SERVER_IP          # must log in without a password prompt for the server
```

### 1.2 Turn off password login

Keep your current SSH session open and use a **second terminal** to test before you close anything.

```bash
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl reload ssh
```

Check from the second terminal: `ssh deploy@YOUR_SERVER_IP` works, and `ssh -o PubkeyAuthentication=no deploy@YOUR_SERVER_IP` is refused.

### 1.3 Firewall (ufw): SSH, 80 and 443 only

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp             # optional: HTTP/3
sudo ufw enable
sudo ufw status verbose
```

Docker publishes ports through iptables and **bypasses ufw**. That is why the compose file publishes only `127.0.0.1:8000` (API) and 80/443 (Caddy), and never the database.

Optional but recommended: `sudo apt -y install unattended-upgrades fail2ban`.

## 2. Install Docker Engine and the Compose plugin

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
```

Log out and in again (so the group applies), then:

```bash
docker --version && docker compose version
docker run --rm hello-world
```

**If `download.docker.com` is unreachable**, install Ubuntu's own packages instead (slightly older, fine for this project):

```bash
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker deploy
```

**If `hello-world` cannot pull** (Docker Hub blocks some countries), see 3.3 ("Images without Docker Hub").

Some hosts have flaky DNS inside containers (symptom: `No address associated with hostname`). Give Docker explicit resolvers and rotate logs:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "dns": ["1.1.1.1", "8.8.8.8"],
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "5"}
}
EOF
sudo systemctl restart docker
```

If those public resolvers are unreachable from your network, use the resolvers cloud.ir gives you (`resolvectl status`).

## 3. Outbound access (do this before installing the project)

The assistant needs to reach: **api.telegram.org**, **the DeepSeek API**, **api.resend.com**, **Google (oauth2.googleapis.com, www.googleapis.com)**. The build also needs Docker Hub, PyPI and alphacephei.com (the Vosk model).

### 3.1 Check

Copy the script to the server (or run it from the clone in section 4) and run it:

```bash
curl -fsSL -o check_outbound.sh https://raw.githubusercontent.com/YOU/REPO/main/scripts/check_outbound.sh   # or scp it
bash check_outbound.sh
```

`OK` means the host answered (even with 401/404). `FAIL` means a timeout or reset. After the stack is installed you can also test from inside the containers, which honours your proxy settings:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps bot python -m app.outbound_check
```

Everything `OK`? Go to section 4. Otherwise pick the fix for each failing host:

### 3.2 Fixes for blocked hosts

**Telegram blocked.** Use an API gateway on a small VPS **outside** the restricted network that you control (never a public relay: it would see your bot token and every message). On that VPS, behind HTTPS, forward to Telegram, for example with nginx:

```nginx
server {
    listen 443 ssl;
    server_name relay.example.com;
    # ssl_certificate / ssl_certificate_key: e.g. from certbot
    location / {
        proxy_pass https://api.telegram.org;
        proxy_ssl_server_name on;
        proxy_set_header Host api.telegram.org;
        proxy_read_timeout 75s;
    }
}
```

Restrict it (nginx `allow YOUR_SERVER_IP; deny all;` or a firewall rule) so only your server can use it. Then in the server's `.env`:

```
TELEGRAM_API_BASE_URL=https://relay.example.com
```

**No VPS abroad? Use a free Cloudflare Worker as the gateway.** `deploy/telegram-gateway-worker.js` forwards only requests for *your* bot token to Telegram (the token is stored as a Worker secret). Steps: Cloudflare dashboard > Workers & Pages > Create > Hello World > paste the file > Deploy; Settings > Variables and Secrets > secret `BOT_TOKEN` = your bot token; Settings > Domains & Routes > add a custom domain on a Cloudflare-managed zone (for example `tg.example.com`; the `workers.dev` address is often unreachable from Iran). Then:

```
TELEGRAM_API_BASE_URL=https://tg.example.com
```

Check from the server before going on: `curl -s https://tg.example.com/bot<YOUR_TOKEN>/getMe` must return `{"ok":true,...}`. Anyone without the token gets a 404. Cloudflare can technically see the traffic, so only use an account you control.

Alternative: an HTTP(S) or SOCKS5 proxy, `TELEGRAM_PROXY_URL=http://user:pass@proxy-host:3128` or `socks5://proxy-host:1080`.

**DeepSeek blocked.** Same idea: a gateway that forwards to `https://api.deepseek.com`, then `DEEPSEEK_BASE_URL=https://relay2.example.com`. Or use the general proxy below.

**Resend / Google blocked.** Use an outbound HTTPS proxy for everything else. In `.env`:

```
HTTPS_PROXY=http://user:pass@proxy-host:3128
HTTP_PROXY=http://user:pass@proxy-host:3128
NO_PROXY=db,dashboard,localhost,127.0.0.1
```

`httpx` (used for DeepSeek, Resend and Google) honours these automatically. `NO_PROXY` keeps container-to-container traffic off the proxy. A simple proxy for your foreign VPS is `tinyproxy` or `squid`, listening only on a private address or reachable only from your server's IP, with a password. The Google refresh token is created on your own computer, so only `oauth2.googleapis.com` and `www.googleapis.com` must be reachable from the server.

After changing `.env`, re-run the outbound check from inside the containers (3.1).

### 3.3 Images without Docker Hub or PyPI

If the server cannot pull images, build and ship them from your own computer (which has access):

```bash
# On your computer, in the project folder (the two variables only satisfy the compose file):
export DASHBOARD_HOST=usage.example.com ACME_EMAIL=you@example.com
docker compose -f docker-compose.prod.yml build
docker save persian-assistant:prod postgres:16.14 caddy:2.8.4-alpine | gzip > assistant-images.tar.gz
scp assistant-images.tar.gz deploy@YOUR_SERVER_IP:~/

# On the server:
gunzip -c ~/assistant-images.tar.gz | docker load
```

Then deploy with `APP_TAG=prod` and skip the build: `APP_TAG=prod docker compose -f docker-compose.prod.yml up -d` (instead of `scripts/deploy.sh`, which rebuilds). For updates, repeat the build and load on your computer.

Vosk model unreachable only: download `vosk-model-small-fa-0.42.zip` elsewhere, put it in `models/` before building (see `models/README.md`).

## 4. Get the code onto the server

### 4.1 Through a private Git remote (recommended)

On your computer:

```bash
git remote add origin git@github.com:YOU/REPO.git
git push -u origin main --tags
```

On the server (a read-only deploy key, so a stolen server cannot push):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/assistant_deploy -N ""
cat ~/.ssh/assistant_deploy.pub     # GitHub: repo Settings > Deploy keys > Add (read-only)
cat >> ~/.ssh/config <<'EOF'
Host github-assistant
    HostName github.com
    User git
    IdentityFile ~/.ssh/assistant_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git clone git@github-assistant:YOU/REPO.git ~/assistant
cd ~/assistant
```

### 4.2 Without a Git host (`git bundle`)

```bash
# On your computer
git bundle create assistant.bundle --all
scp assistant.bundle deploy@YOUR_SERVER_IP:~/

# On the server
git clone ~/assistant.bundle ~/assistant
cd ~/assistant
git remote set-url origin ~/assistant.bundle
```

For every later update, make a new bundle on your computer, `scp` it to `~/assistant.bundle` (same path), then run `scripts/deploy.sh`: its `git pull` reads the bundle.

Check: `cd ~/assistant && git log --oneline | head -3` and `ls scripts docs`.

## 5. Configure `.env` and secrets (secrets never go through git)

```bash
cd ~/assistant
cp .env.example .env
chmod 600 .env
mkdir -p secrets && chmod 700 secrets
```

Generate strong values (hex is safe for every field):

```bash
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "DASHBOARD_DB_PASSWORD=$(openssl rand -hex 24)"
echo "DASHBOARD_SECRET_KEY=$(openssl rand -hex 32)"
echo "ADMIN_API_KEY=$(openssl rand -hex 32)"
```

Edit `.env` (`nano .env`) and set at least:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` | from section 0 |
| `OWNER_NAME`, `OWNER_EMAIL` | you |
| `DEEPSEEK_API_KEY` | key |
| `RESEND_API_KEY`, `EMAIL_FROM` | key, verified sender |
| `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID` | token from your computer, `primary` |
| `POSTGRES_PASSWORD`, `POSTGRES_USER`, `POSTGRES_DB` | generated password; keep the other two |
| `DASHBOARD_DB_PASSWORD`, `DASHBOARD_SECRET_KEY`, `ADMIN_API_KEY` | generated above |
| `DASHBOARD_HOST` | `usage.example.com` |
| `ACME_EMAIL` | your email (Let's Encrypt notices) |
| `DASHBOARD_COOKIE_SECURE` | `true` |
| `DASHBOARD_DEMO` | `0` (no demo data in production) |
| `PROXY_MODE`, `DASHBOARD_PORT` | `caddy` if ports 80/443 are free, otherwise `nginx` and a free local port (section 6b) |
| `DAILY_TOKEN_BUDGET`, `MONTHLY_TOKEN_BUDGET`, `BUDGET_HARD_STOP` | optional limits; the bot alerts you at 80% and 100% |
| Section 3 values | `TELEGRAM_API_BASE_URL`, `HTTPS_PROXY`, ... only if needed |

Leave `DEBUG=false`. Copy the Google OAuth client file from **your computer**:

```bash
# On your computer
scp secrets/credentials.json deploy@YOUR_SERVER_IP:~/assistant/secrets/credentials.json
# On the server
chmod 600 ~/assistant/secrets/credentials.json
```

The refresh token is made on your computer (`python google_auth.py`, see the README). If your Google OAuth consent screen is still in *Testing* mode the token expires after 7 days: publish the app first.

**Dashboard password.** Build the image once, then create the hash (it prompts twice and prints one line):

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm --no-deps bot python -m app.dashboard.hashpw
```

Paste the printed `DASHBOARD_PASSWORD_HASH=...` line into `.env`.

The Vosk Persian model needs no step: it is downloaded **into the image** during the build (or taken from `models/`).

Check: `grep -c '=' .env` is not zero, `ls -l .env` shows `-rw-------`, and `git status` does not list `.env` or `secrets/`.

## 6. DNS

At your DNS provider create an **A record**:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `usage` (for `usage.example.com`) | `YOUR_SERVER_IP` | 300 |

Add an `AAAA` record only if the server really has working IPv6. Check propagation from your computer:

```bash
dig +short usage.example.com        # must print YOUR_SERVER_IP
```

Caddy gets the Let's Encrypt certificate through port 80, so DNS must already point at the server and ports 80/443 must be reachable from the internet. If Let's Encrypt cannot reach the server (some networks block inbound traffic from abroad), put the dashboard behind a Cloudflare Tunnel or similar instead; the dashboard itself still requires its password.

## 6b. If nginx (or Apache) already uses ports 80/443

Caddy needs those ports for itself. If something else owns them (check with `sudo ss -tlnp | grep -E ':(80|443)\s'`), keep it and let it forward to the dashboard instead:

1. In `.env` set `PROXY_MODE=nginx` and `DASHBOARD_PORT=8081` (any free local port; check `sudo ss -tlnp | grep 8081`). Do not start Caddy: `scripts/deploy.sh` skips it in this mode, and `DASHBOARD_COOKIE_SECURE=true` stays on because users reach the site over HTTPS.
2. Add a virtual host (adjust `server_name` and the port in the file if needed) and get the certificate with certbot:

```bash
sudo cp deploy/nginx-dashboard.conf /etc/nginx/sites-available/assistant-dashboard
sudo nano /etc/nginx/sites-available/assistant-dashboard
sudo ln -s /etc/nginx/sites-available/assistant-dashboard /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d usage.example.com
```

The dashboard container publishes only `127.0.0.1:DASHBOARD_PORT`, so it is never reachable except through your nginx. The rest (sections 7 onward) is identical; ignore the Caddy lines.

## 7. First start

```bash
cd ~/assistant
scripts/deploy.sh --no-pull
```

This builds the image (a few minutes the first time), runs the migrations (`migrate` service: `alembic upgrade head`, then creates the dashboard's read-only database role), starts everything and waits until every service is healthy. Then check:

```bash
docker compose -f docker-compose.prod.yml ps                  # db, bot, api, dashboard, proxy: healthy
docker compose -f docker-compose.prod.yml logs --tail 30 bot # "Application started"
docker compose -f docker-compose.prod.yml logs --tail 30 migrate
curl -s http://127.0.0.1:8000/health                          # {"status":"ok", database/telegram/llm all ok}
curl -sI https://usage.example.com/login | head -3            # HTTP/2 200
docker compose -f docker-compose.prod.yml logs proxy | grep -i "certificate obtained"   # Caddy mode only
```

Open `https://usage.example.com`, sign in with the dashboard password, and you should see the (empty) dashboard.

Create the owner row and the sample contacts, then check the email key:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps bot python -m app.cli seed
docker compose -f docker-compose.prod.yml run --rm --no-deps bot python -m app.cli send-test-email
```

The API's admin endpoints are reachable from the server itself, or from your computer through a tunnel: `ssh -L 8000:127.0.0.1:8000 deploy@YOUR_SERVER_IP`, then `curl -H "X-Admin-Key: ..." http://127.0.0.1:8000/runs`.

## 8. Acceptance test on the server

1. In Telegram send `/start` to the bot: it answers in Persian.
2. Send a voice message: "فردا ساعت ده صبح با علی جلسه بذار". The bot shows the transcript and a preview with **تأیید / ویرایش / لغو**.
3. Tap **تأیید**: the event appears in Google Calendar (and invitations go out to guests).
4. Send a voice message asking for an email to a contact: preview, **تأیید**, and the email arrives.
5. In the dashboard the runs and token usage appear within seconds (`/logs`, `/`).

If a step fails, see section 12.

## 9. Backups (nightly, kept off the server) and a tested restore

The database holds transcripts, contacts and your audit trail. `scripts/backup.sh` dumps it, verifies the archive, keeps the last 7 locally and copies the dump to another place.

Configure the off-server destination in `scripts/backup.conf` (plain shell, not in git). Pick one:

```bash
cat > scripts/backup.conf <<'EOF'
# Option A: another server over SSH (create a key without passphrase restricted to that host)
BACKUP_SCP_TARGET=backup@other-host:/srv/backups/assistant
# Option B: S3-compatible storage through rclone ("rclone config" once)
# BACKUP_RCLONE_REMOTE=myremote:assistant-backups
# Encrypt the off-server copy (recommended): apt install age; age-keygen -o key.txt; keep key.txt away from the server
# BACKUP_AGE_RECIPIENT=age1xxxxxxxx
EOF
chmod 600 scripts/backup.conf
scripts/backup.sh --require-offsite     # run it once by hand; it must say "copied to ..."
ls -l backups/
```

Nightly at 03:15 as the deploy user:

```bash
( crontab -l 2>/dev/null; echo '15 3 * * * cd /home/deploy/assistant && scripts/backup.sh --require-offsite >> /home/deploy/backup.log 2>&1' ) | crontab -
crontab -l
```

`--require-offsite` makes the job fail loudly (non-zero exit, a line in `backup.log`) if the off-server copy did not happen. Look at `tail ~/backup.log` now and then.

**Test the restore** (do it now, then monthly). This restores into a scratch database, prints row counts and drops it; the live database is not touched:

```bash
scripts/restore.sh backups/assistant-XXXX.dump
```

The row counts must match what you expect (compare with the dashboard or `psql`). If you keep encrypted copies, fetch one from the other place and decrypt it first: `age -d -i key.txt assistant-XXXX.dump.age > assistant-XXXX.dump`.

**Real recovery** (replaces the live database; it asks you to type the database name):

```bash
scripts/restore.sh backups/assistant-XXXX.dump --replace
```

It stops the bot/api/dashboard, restores, re-runs migrations and the dashboard role, and starts everything again. Rebuilding a lost server: sections 1 to 7, then restore the newest dump with `--replace` (the database exists and is empty after the first start).

## 10. Updates and rollback

Update (safe to run twice; with nothing new it changes nothing):

```bash
cd ~/assistant
scripts/deploy.sh
```

It takes a backup, runs `git pull --ff-only`, builds the image tagged with the commit, applies migrations, restarts changed services and waits for health. If the new version is not healthy it exits non-zero and tells you what to run.

By hand, the same steps are: `git pull --ff-only && docker compose -f docker-compose.prod.yml build && docker compose -f docker-compose.prod.yml up -d`.

**Rollback to the previous commit:**

```bash
scripts/deploy.sh rollback          # checks out the previous commit and redeploys it
scripts/deploy.sh status            # what is deployed
# later, to go back to the latest code:
git checkout main && scripts/deploy.sh
```

**Rollback and migrations.** The code rolls back, the database does not. Most releases only add tables or columns, which older code ignores, so a plain rollback is fine. If the newer release changed or dropped something that the old code needs, you have two options:

- Restore the backup `deploy.sh` made just before the update: `scripts/restore.sh backups/assistant-<time>.dump --replace` (you lose data written since).
- Or step the schema back with the **newer** image, before rolling the code back: `APP_TAG=<newer-commit> docker compose -f docker-compose.prod.yml run --rm --no-deps migrate alembic downgrade -1`.

Old images stay on disk (one per deployed commit) so rollbacks do not need a rebuild. Free space now and then: `docker image prune -a --filter "until=720h"`.

## 11. Day-to-day

```bash
docker compose -f docker-compose.prod.yml logs -f bot              # follow the bot (JSON lines with run_id)
docker compose -f docker-compose.prod.yml logs --since 1h api | grep '"level": "ERROR"'
docker compose -f docker-compose.prod.yml restart bot
docker stats --no-stream                                           # memory and CPU against the limits
docker system df                                                   # disk used by images and volumes
```

Every log line of one request shares a `run_id` (bot, agent, tools). Search with `grep '"run_id": 123'`. Logs rotate at 10 MB x 5 files per container.

Rotating a secret: edit `.env`, then `docker compose -f docker-compose.prod.yml up -d --force-recreate <service>`. For `POSTGRES_PASSWORD`, change it in PostgreSQL first (`ALTER USER ... PASSWORD ...`), then in `.env`.

Budgets: set `DAILY_TOKEN_BUDGET` / `MONTHLY_TOKEN_BUDGET` in `.env` and recreate the bot, api and dashboard; `BUDGET_HARD_STOP=true` makes the bot refuse new requests at 100%.

Certificates renew automatically (Caddy keeps them in the `caddy_data` volume; do not delete that volume).

## 12. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Bot never answers | `docker compose ... logs bot`. Your Telegram id must be in `ALLOWED_USER_IDS`. If logs show `ConnectError`/timeouts to Telegram, fix section 3 (gateway or proxy) |
| `No address associated with hostname` in logs | DNS in containers: set `"dns"` in `/etc/docker/daemon.json` (section 2) and restart Docker. The LLM client retries, so it is usually transient |
| `bot` shows `unhealthy` | the heartbeat file is stale: the event loop is stuck or the process is starved. `logs bot`, then `restart bot` |
| Voice recognised badly | the small Vosk model is weak; a larger Vosk model or another STT service improves it (see README). Low-confidence audio gets a "please repeat" |
| "Unknown tool" or no tools used | you deployed an old image: `scripts/deploy.sh` and check `docker image ls persian-assistant` |
| Calendar error `invalid_grant` | refresh token expired or revoked: run `python google_auth.py` on your computer again, update `GOOGLE_REFRESH_TOKEN`, recreate `bot` |
| Calendar error 403 | the Calendar API is not enabled in your Google Cloud project, or the scope is wrong |
| Email error 403 / 422 | `EMAIL_FROM` is not on a domain verified in Resend |
| Dashboard certificate error | DNS does not point at the server yet, or 80/443 blocked. `docker compose ... logs proxy` shows the ACME error. Fix DNS or the firewall (ufw **and** the cloud.ir security group) |
| Dashboard `503` "not configured" | `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SECRET_KEY` or `DASHBOARD_DB_PASSWORD` is empty in `.env` |
| Dashboard login always fails | the hash was pasted with a typo or quotes; regenerate it (section 5). After 5 failures an IP is locked out for 5 minutes |
| `permission denied` in dashboard logs | the read-only role is missing: `docker compose ... run --rm migrate` recreates it |
| API `/health` returns 503 | the JSON says which part is down: `database`, `telegram` or `llm` |
| Disk filling up | `docker system df`; prune old images (section 10); old backups are pruned by `backup.sh` (`KEEP_LOCAL`) |

## 13. Known limits

- One bot process (rate limits and the Telegram conversation state are in memory; a restart forgets a pending "Edit").
- Speech recognition uses the small Vosk Persian model and is the weakest link; the agent evaluation (`docs/EVAL.md`) measures the text understanding only.
- Single server, no high availability: the backups are your safety net.
- The MCP endpoint has no authentication of its own; it is off in production unless you enable the `mcp` profile, and it only listens on `127.0.0.1`.
