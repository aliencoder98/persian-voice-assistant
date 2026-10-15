"""Can this machine reach what the assistant needs? Honors HTTPS_PROXY / TELEGRAM_PROXY_URL / gateways.

    docker compose -f docker-compose.prod.yml run --rm --no-deps bot python -m app.outbound_check
"""

import sys
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings


@dataclass
class Target:
    name: str
    url: str
    note: str = ""
    proxy: str | None = None
    ok_below: int = 500  # any HTTP answer below this counts as reachable (401/404 still prove connectivity)


def targets() -> list[Target]:
    s = get_settings()
    tg_base = (s.telegram_api_base_url or "https://api.telegram.org").rstrip("/")
    ds_base = s.deepseek_base_url.rstrip("/")
    return [
        Target("Telegram Bot API", f"{tg_base}/", "set TELEGRAM_PROXY_URL or TELEGRAM_API_BASE_URL if blocked", s.telegram_proxy_url or None),
        Target("DeepSeek API", f"{ds_base}/models", "set DEEPSEEK_BASE_URL to a gateway, or HTTPS_PROXY"),
        Target("Resend API", "https://api.resend.com/", "set HTTPS_PROXY if blocked"),
        Target("Google OAuth", "https://oauth2.googleapis.com/token", "needed for Calendar; set HTTPS_PROXY if blocked"),
        Target("Google Calendar API", "https://www.googleapis.com/calendar/v3/users/me/calendarList", "needed for Calendar"),
    ]


def check(target: Target, client: httpx.Client | None = None) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        if client is not None:
            r = client.get(target.url)
        else:
            r = httpx.get(target.url, timeout=10, proxy=target.proxy, follow_redirects=True)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"
    ms = int((time.perf_counter() - started) * 1000)
    return r.status_code < target.ok_below, f"HTTP {r.status_code} in {ms} ms"


def main() -> int:
    failed = 0
    for t in targets():
        ok, detail = check(t)
        failed += not ok
        print(f"[{'OK ' if ok else 'FAIL'}] {t.name:<22} {detail}" + ("" if ok else f"   -> {t.note}"))
    print("\nAll reachable." if not failed else f"\n{failed} unreachable: see docs/DEPLOY.md, section 'Outbound access'.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
