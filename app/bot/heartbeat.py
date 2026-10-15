"""Liveness for Docker: the bot touches a file every 30 s; the healthcheck fails if it goes stale.

The bot has no HTTP port (long polling), so a stuck event loop is only visible through this file.
    python -m app.bot.heartbeat --check
"""

import asyncio
import os
import sys
import time
from pathlib import Path

INTERVAL_S = 30
MAX_AGE_S = 120


def touch(path: str) -> None:
    Path(path).touch()


async def heartbeat_loop(path: str, interval_s: float = INTERVAL_S) -> None:
    while True:
        try:
            await asyncio.to_thread(touch, path)
        except OSError:
            pass  # a read-only /tmp shows up as an unhealthy container, not a crash
        await asyncio.sleep(interval_s)


def is_fresh(path: str, max_age_s: float = MAX_AGE_S, now: float | None = None) -> bool:
    try:
        return (now or time.time()) - os.stat(path).st_mtime <= max_age_s
    except OSError:
        return False


def main() -> int:
    from app.config import get_settings

    return 0 if is_fresh(get_settings().bot_heartbeat_file) else 1


if __name__ == "__main__":
    sys.exit(main())
