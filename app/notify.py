"""Out-of-band Telegram messages (API retry previews, budget alerts). Sync; call from a thread."""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger("notify")


def telegram_send(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    s = get_settings()
    if not s.telegram_bot_token:
        return False
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json=payload,
            timeout=15,
            proxy=s.telegram_proxy_url or None,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # Never log the exception text: for HTTP errors it embeds the URL, which contains the token.
        log.warning("Telegram sendMessage failed (%s)", type(exc).__name__)
        return False
