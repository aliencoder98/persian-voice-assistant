#!/usr/bin/env python3
"""Build the production .env from your local .env, on YOUR computer (nothing is sent anywhere).

    python scripts/make_server_env.py

Writes secrets/server.env (git-ignored): your keys and tokens are carried over, production settings
are applied, and fresh random secrets are generated for the new server (database password, dashboard
password and session key, admin API key). The new dashboard password goes to
secrets/server_dashboard_password.txt. Values are never printed.

Then copy it to the server as .env:   scp secrets/server.env deploy@SERVER_IP:/home/deploy/assistant/.env
Fill in the CHANGE_ME values first (domain names).
"""

import pathlib
import re
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.dashboard.auth import hash_password  # noqa: E402  (stdlib only)


def read_env(path: pathlib.Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    local_path = ROOT / ".env"
    if not local_path.exists():
        print("No local .env to start from.", file=sys.stderr)
        return 1
    local = read_env(local_path)
    template = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    warnings: list[str] = []

    dashboard_password = secrets.token_urlsafe(14)
    new = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "DASHBOARD_DB_PASSWORD": secrets.token_hex(24),
        "DASHBOARD_SECRET_KEY": secrets.token_hex(32),
        "ADMIN_API_KEY": secrets.token_hex(32),
        "DASHBOARD_PASSWORD_HASH": hash_password(dashboard_password),
    }
    production = {
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "AUDIO_DIR": "/data/audio",
        "API_PORT": "8000",
        "MCP_PORT": "8765",
        "DASHBOARD_PORT": "8081",
        "PROXY_MODE": "nginx",
        "DASHBOARD_COOKIE_SECURE": "true",
        "DASHBOARD_DEMO": "0",
        "DEBUG": "false",
        "LOG_LEVEL": "INFO",
        "DASHBOARD_HOST": "CHANGE_ME_yourdomain.com",
        "DASHBOARD_BASE_PATH": "",
        "ACME_EMAIL": local.get("OWNER_EMAIL") or "CHANGE_ME_you@example.com",
        "TELEGRAM_API_BASE_URL": "CHANGE_ME_https://tg.yourdomain.com",
        "GOOGLE_CREDENTIALS_FILE": "",
    }

    final: dict[str, str] = {}
    for line in template:
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if not m or line.lstrip().startswith("#"):
            continue
        key = m.group(1)
        if key in new:
            final[key] = new[key]
        elif key in production:
            final[key] = production[key]
        elif key in local:
            final[key] = local[key]
        else:
            final[key] = line.split("=", 1)[1]
    final.pop("GOOGLE_CREDENTIALS_FILE", None)

    # sanity checks that would otherwise cost a failed deploy
    for key in ("DEEPSEEK_MAX_RETRIES", "DEEPSEEK_TIMEOUT_S", "MAX_VOICE_SECONDS", "RATE_LIMIT_PER_MINUTE", "AUDIO_MAX_AGE_S"):
        if key in final and not re.fullmatch(r"\d+", final[key]):
            warnings.append(f"{key} was not a number in your local .env: reset to the default")
            final[key] = {"DEEPSEEK_MAX_RETRIES": "3", "DEEPSEEK_TIMEOUT_S": "30", "MAX_VOICE_SECONDS": "60",
                          "RATE_LIMIT_PER_MINUTE": "10", "AUDIO_MAX_AGE_S": "3600"}[key]  # fmt: skip
    if not re.fullmatch(r"sk-[A-Za-z0-9]{20,48}", final.get("DEEPSEEK_API_KEY", "")):
        warnings.append("DEEPSEEK_API_KEY does not look like a valid key (expected sk- followed by letters/digits): replace it")
    for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "RESEND_API_KEY", "EMAIL_FROM", "GOOGLE_REFRESH_TOKEN"):
        if not final.get(key):
            warnings.append(f"{key} is empty")
    if not (ROOT / "secrets" / "credentials.json").exists():
        warnings.append("secrets/credentials.json is missing (the Google OAuth client file)")

    out_dir = ROOT / "secrets"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "server.env"
    out.write_text("".join(f"{k}={v}\n" for k, v in final.items()), encoding="utf-8")
    (out_dir / "server_dashboard_password.txt").write_text(dashboard_password + "\n", encoding="utf-8")

    todo = [k for k, v in final.items() if "CHANGE_ME" in v]
    print(f"Wrote {out.relative_to(ROOT)} ({len(final)} variables).")
    print(f"New dashboard password: secrets/server_dashboard_password.txt")
    print("Fill in before uploading:", ", ".join(todo) or "nothing")
    for w in warnings:
        print("WARNING:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
