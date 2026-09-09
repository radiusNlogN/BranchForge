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

    # --- GitHub inspection (milestone 2) ------------------------------------
    # The worker reads public repositories unauthenticated, which GitHub limits
    # to roughly 60 requests/hour per IP.
    github_api_base_url: str = "https://api.github.com"
    github_user_agent: str = "BranchForge/0.2"

    github_request_timeout_seconds: float = 10.0
    github_connect_timeout_seconds: float = 5.0

    # Hard ceiling on requests per inspection: 3 base requests (repository
    # metadata, commit resolution, tree) plus one per file fetched.
    github_max_requests: int = 20

    # Raw HTTP response body caps, enforced while streaming and *before* JSON
    # parsing. Metadata sizes alone do not bound a download, so these are the
    # real defence against an oversized upstream response.
    # GitHub caps a recursive tree at 7 MB, so 8 MB leaves headroom.
    github_max_response_bytes: int = 8_000_000
    # The contents endpoint returns base64, which inflates by 4/3, plus newlines
    # and a JSON envelope. 256 KiB comfortably covers the 100 KB decoded cap.
    github_max_content_response_bytes: int = 262_144

    # Decoded-content budgets, separate from the raw response caps above.
    github_max_file_bytes: int = 100_000
    github_max_total_content_bytes: int = 400_000

    # Report shaping.
    github_max_files_listed: int = 500
    github_max_files_fetched: int = 8

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
