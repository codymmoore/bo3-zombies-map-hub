from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://hub:hub@localhost:5432/hub"

    # Web app
    secret_key: str = "change-me"
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
    internal_api_token: str = "change-me-too"

    # Steam
    steam_api_key: str | None = None
    # Black Ops III. Set to empty to accept any app.
    steam_app_id: str | None = "311210"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
