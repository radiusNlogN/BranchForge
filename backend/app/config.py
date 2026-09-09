"""Application configuration, sourced from the environment.

Every deployment knob lives here so nothing else in the codebase reads os.environ
directly. DATABASE_URL is the single source of truth for the database, shared by
the app and by Alembic.
"""

from functools import lru_cache

from pydantic import SecretStr
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

    # --- Patch-proposal agent (milestone 3) ---------------------------------
    # Read from ANTHROPIC_API_KEY. SecretStr so the value cannot be printed by
    # an accidental repr of settings; it is never logged, persisted, or returned.
    anthropic_api_key: SecretStr | None = None
    # No model identifier is invented here; override with ANTHROPIC_MODEL.
    anthropic_model: str = "claude-opus-5"

    # Implicit retries are disabled for this milestone: a retry would silently
    # re-spend budget. NOTE: the timeout below is per HTTP request, not a
    # deadline for the whole attempt — turn and tool-call limits bound that.
    agent_request_timeout_seconds: float = 120.0

    agent_max_turns: int = 8
    agent_max_tool_calls: int = 12

    agent_max_file_bytes: int = 60_000
    agent_max_total_fetched_bytes: int = 200_000

    # Output tokens per model call. Deliberately not lowballed — a response cut
    # off at max_tokens is recorded as a failure, never stored as a patch.
    agent_max_output_tokens: int = 16_000

    # Context accounting uses the provider's token-counting endpoint before each
    # generation. The usable input budget is the model's context window minus the
    # output reservation and a safety margin.
    agent_model_context_tokens: int = 1_000_000
    agent_context_safety_margin_tokens: int = 8_000

    # Bounds on everything persisted.
    agent_max_patch_bytes: int = 60_000
    agent_max_summary_chars: int = 4_000
    agent_max_test_command_chars: int = 500
    agent_max_error_chars: int = 2_000
    agent_max_events: int = 200
    agent_max_event_detail_chars: int = 2_000

    @property
    def agent_max_input_tokens(self) -> int:
        """Usable input budget: context window less output reservation and margin."""
        return (
            self.agent_model_context_tokens
            - self.agent_max_output_tokens
            - self.agent_context_safety_margin_tokens
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
