import httpx

from app.config import get_settings

RESEND_URL = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    pass


def send_email(to: str, subject: str, body: str, *, html: str | None = None) -> str:
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

    try:
        response = httpx.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Resend: {exc}") from exc

    if response.status_code >= 400:
        raise EmailError(f"Resend returned {response.status_code}: {response.text}")
    return response.json()["id"]
