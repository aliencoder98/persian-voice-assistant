import hmac
from typing import Iterator

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import SessionLocal

_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def require_admin(key: str | None = Security(_key_header)) -> None:
    configured = get_settings().admin_api_key
    if not configured:
        raise HTTPException(status_code=503, detail="Admin API is disabled (ADMIN_API_KEY is not set).")
    if not key or not hmac.compare_digest(key.encode(), configured.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key.")


AdminOnly = Depends(require_admin)


def scrub(text: str | None) -> str | None:
    """Remove configured secrets from free text before it leaves the API."""
    if not text:
        return text
    s = get_settings()
    for secret in (s.telegram_bot_token, s.resend_api_key, s.deepseek_api_key, s.google_client_secret,
                   s.google_refresh_token, s.admin_api_key, s.postgres_password):  # fmt: skip
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text
