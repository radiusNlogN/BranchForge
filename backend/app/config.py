"""Application configuration, sourced from the environment.

Every deployment knob lives here so nothing else in the codebase reads os.environ
directly. DATABASE_URL is the single source of truth for the database, shared by
the app and by Alembic.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./branchforge.db"

    # Stored as a raw string rather than a list: pydantic-settings would otherwise
    # insist on JSON for a list-typed field, and a comma-separated env var is far
    # friendlier to write by hand.
    cors_origins: str = DEFAULT_CORS_ORIGINS

    run_list_default_limit: int = 25
    run_list_max_limit: int = 100

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
