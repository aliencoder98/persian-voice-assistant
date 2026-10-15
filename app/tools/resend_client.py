import httpx

from app.config import get_settings
from app.tools.http_retry import retry_connect

RESEND_URL = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    pass


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Send one email via Resend and return the provider message id."""
    settings = get_settings()
    if not settings.resend_api_key or not settings.email_from:
        raise EmailError("RESEND_API_KEY and EMAIL_FROM must be set.")

    payload: dict = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if html:
        payload["html"] = html

    headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key  # Resend dedupes repeats for 24h

    try:
        response = retry_connect(
            lambda: httpx.post(RESEND_URL, json=payload, headers=headers, timeout=20)
        )
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Resend: {exc}") from exc

    if response.status_code >= 400:
        raise EmailError(f"Resend returned {response.status_code}: {response.text}")
    return response.json()["id"]
