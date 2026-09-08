import logging
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("app.config")

PLACEHOLDER_SECRET_KEY = "change-me"
PLACEHOLDER_INTERNAL_TOKEN = "change-me-too"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # "prod" refuses to start with placeholder/short secrets; "dev" only warns.
    env: Literal["dev", "prod"] = "dev"

    # Database
    database_url: str = "postgresql+psycopg://hub:hub@localhost:5432/hub"

    # Web app
    secret_key: str = PLACEHOLDER_SECRET_KEY
    frontend_url: str = "http://localhost:5173"
    # Comma-separated list of allowed CORS origins.
    cors_origins: str = "http://localhost:5173"
    session_cookie_name: str = "hub_session"
    session_max_age_days: int = 30
    cookie_secure: bool = False

    # Discord OAuth2 (identify scope only) + bot token for REST membership lookups
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = "http://localhost:8000/auth/discord/callback"
    discord_bot_token: str = ""
    discord_api_base: str = "https://discord.com/api/v10"
    membership_cache_ttl_seconds: int = 90

    # Shared secret between the backend and the bot service (X-Internal-Token header)
    internal_api_token: str = PLACEHOLDER_INTERNAL_TOKEN

    # Steam
    steam_api_key: str | None = None
    # Black Ops III. Set to empty to accept any app.
    steam_app_id: str | None = "311210"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def secret_problems(self) -> list[str]:
        """Why this configuration must not face the internet, if anything."""
        problems = []
        if self.secret_key == PLACEHOLDER_SECRET_KEY or len(self.secret_key) < 32:
            problems.append("SECRET_KEY is the placeholder or shorter than 32 characters (forged login cookies)")
        if self.internal_api_token == PLACEHOLDER_INTERNAL_TOKEN or len(self.internal_api_token) < 16:
            problems.append("INTERNAL_API_TOKEN is the placeholder or shorter than 16 characters (open /internal/*)")
        return problems

    @model_validator(mode="after")
    def _guard_secrets(self) -> "Settings":
        problems = self.secret_problems()
        if not problems:
            return self
        if self.env == "prod":
            raise ValueError("Refusing to start with insecure settings: " + "; ".join(problems))
        for p in problems:
            log.warning("insecure dev setting: %s", p)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
