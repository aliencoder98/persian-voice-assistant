from functools import lru_cache

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
    log_level: str = "INFO"

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
