import json
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    allowed_user_ids: str = ""

    owner_name: str = "Owner"
    owner_email: str = ""
    default_timezone: str = "Asia/Tehran"

    resend_api_key: str = ""
    email_from: str = ""

    postgres_user: str = "assistant"
    postgres_password: str = ""
    postgres_db: str = "assistant"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    audio_dir: str = "/data/audio"

    stt_model_path: str = "/opt/vosk/model"
    stt_min_confidence: float = 0.5

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_s: float = 30.0
    deepseek_max_retries: int = 3

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    log_level: str = "INFO"

    # Google "Desktop app" client file (downloaded from Cloud Console); used when the
    # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET variables are empty.
    google_credentials_file: str = "secrets/credentials.json"

    @model_validator(mode="after")
    def _google_client_from_file(self) -> "Settings":
        path = Path(self.google_credentials_file)
        if (not self.google_client_id or not self.google_client_secret) and path.is_file():
            try:
                client = json.loads(path.read_text(encoding="utf-8"))["installed"]
                self.google_client_id = self.google_client_id or client["client_id"]
                self.google_client_secret = self.google_client_secret or client["client_secret"]
            except (ValueError, KeyError):
                pass  # malformed file: Google calls will report missing credentials
        return self

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(
            int(part) for part in self.allowed_user_ids.replace(" ", "").split(",") if part
        )

    @property
    def database_url(self) -> URL:
        # Built from parts so passwords with special characters need no escaping.
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
