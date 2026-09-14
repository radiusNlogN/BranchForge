"""Application configuration, sourced from the environment.

Every deployment knob lives here so nothing else in the codebase reads os.environ
directly. DATABASE_URL is the single source of truth for the database, shared by
the app and by Alembic.
"""

import os
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./branchforge.db"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        """Point a hosted provider's plain Postgres URL at the installed driver.

        Providers hand out `postgres://` or `postgresql://`. SQLAlchemy rejects the
        first outright and maps the second to psycopg2, which is not installed —
        psycopg 3 is. An explicit `postgresql+<driver>://` is left alone.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

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
    # When the turn budget runs out and the most recent turn was a rejected
    # submission, this many extra turns follow in which only `submit_patch` is
    # accepted. Without them, a patch rejected on the final turn could never be
    # corrected even though validation told the model exactly what was wrong.
    agent_max_patch_repair_turns: int = Field(default=2, ge=0)

    agent_max_file_bytes: int = 60_000
    agent_max_total_fetched_bytes: int = 200_000

    # Output tokens per model call. Deliberately not lowballed — a response cut
    # off at max_tokens is recorded as a failure, never stored as a patch.
    agent_max_output_tokens: int = 16_000

    # Context accounting uses the provider's token-counting endpoint before each
    # generation. Two independent ceilings apply and the smaller one wins:
    #   1. an explicit application limit, so an attempt stays small regardless of
    #      how large a context the chosen model happens to offer, and
    #   2. the model's own capacity, less the output reservation and a margin.
    # Keeping (1) explicit matters: swapping in a bigger model must not silently
    # licence a far bigger attempt.
    agent_max_context_tokens: int = 150_000
    agent_model_context_tokens: int = 1_000_000
    agent_context_safety_margin_tokens: int = 8_000

    # Bounds on everything persisted.
    agent_max_patch_bytes: int = 60_000
    agent_max_summary_chars: int = 4_000
    agent_max_test_command_chars: int = 500
    agent_max_error_chars: int = 2_000
    agent_max_events: int = 200
    agent_max_event_detail_chars: int = 2_000

    # --- Patch verification (milestone 4) -----------------------------------
    # The runner image. Build it with:
    #   docker build -t branchforge-runner-python:1 backend/runner/python-pytest
    # The tag is resolved to an immutable image ID once per verification, and
    # that exact ID runs every phase.
    verify_profile: str = "python-pytest"
    verify_image: str = "branchforge-runner-python:1"
    verify_docker_binary: str = "docker"

    # Source snapshot acquisition (codeload archive of one exact commit).
    verify_max_download_bytes: int = 80_000_000
    # Counted on the DECOMPRESSED stream including tar headers, so a compression
    # bomb is bounded by what reading it costs us.
    verify_max_decompressed_bytes: int = 400_000_000
    verify_max_snapshot_files: int = 20_000
    verify_snapshot_deadline_seconds: float = 180.0

    # Container ceilings.
    verify_container_memory: str = "512m"
    verify_container_cpus: str = "1.0"
    verify_container_pids: int = 256
    verify_container_tmpfs_bytes: int = 64 * 1024 * 1024
    verify_test_timeout_seconds: float = 300.0
    verify_cleanup_timeout_seconds: float = 30.0
    verify_git_timeout_seconds: float = 60.0

    # Bounds on everything captured or persisted.
    verify_max_log_bytes: int = 200_000
    verify_max_report_bytes: int = 5_000_000
    verify_report_max_tests: int = 5_000
    verify_report_max_message_chars: int = 400
    verify_max_error_chars: int = 2_000

    # --- Orchestration (milestone 5) ----------------------------------------
    # How many attempt pipelines ONE orchestrator process runs at once. The
    # effective value is min(this, the run's max_parallel_attempts). It is not a
    # global limit: two orchestrators for two runs each get their own.
    orchestrator_max_concurrency: int = Field(default=2, ge=1, le=3)
    # On shutdown: how long a child gets after SIGTERM before SIGKILL.
    orchestrator_child_grace_seconds: float = 10.0
    # After a child exits, how long its output reader may take to hit EOF.
    orchestrator_drain_timeout_seconds: float = 10.0
    # Child output is relayed line by line; longer lines are cut to this.
    orchestrator_max_relayed_line_chars: int = 2_000

    # --- Execution queue (milestone 6) --------------------------------------
    # Whether this deployment runs a dispatcher at all. Set false where none can
    # exist (a serverless host has no Docker and no long-lived process): Start is
    # then refused rather than queueing a job that would wait forever, and the
    # dashboard says why. This is a statement about the deployment, not a probe —
    # true does not prove a dispatcher is running right now.
    dispatcher_available: bool = True
    # The dispatcher drains queued jobs one at a time. These are dispatcher
    # policy, not per-orchestration execution settings, so they are deliberately
    # NOT frozen onto an orchestration (see FROZEN_PREFIXES below).
    dispatcher_poll_seconds: float = 2.0
    # How often an active job's cancellation flag is re-read while a child runs.
    dispatcher_cancel_poll_seconds: float = 0.5
    # Seconds a child gets after SIGTERM before SIGKILL. This MUST exceed the
    # orchestrator's own shutdown budget — it spends up to
    # orchestrator_child_grace_seconds + orchestrator_drain_timeout_seconds +
    # verify_cleanup_timeout_seconds saving a comparison and sweeping its
    # containers. Killing it sooner would throw that work away and leave
    # containers behind for the dispatcher to find.
    dispatcher_child_grace_seconds: float = 90.0
    # After a child exits, how long its output reader may take to hit EOF.
    dispatcher_drain_timeout_seconds: float = 10.0
    # How long an orphaned attempt worker gets after SIGTERM before SIGKILL.
    dispatcher_worker_grace_seconds: float = 10.0
    # Bound on the operator notes kept on one job, so repeated dispatcher
    # restarts cannot grow the row without limit.
    dispatcher_max_job_notes: int = 20

    def dispatcher_lock_path(self) -> str:
        """The canonical lock-file path for this database.

        Derived from the *resolved* database file, never from a separate setting:
        two spellings of one database (`./branchforge.db`, `branchforge.db`, a
        symlink, a relative path from another directory) must map to the same lock
        file, or two dispatchers would each hold "the" lock and drain the same
        queue. There is deliberately no override for that reason.

        `flock` is per-host, so this enforces one dispatcher per database *on this
        machine*. Networked filesystems do not preserve those semantics, which is
        why this is documented as a single-host deployment.
        """
        url = make_url(self.database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            raise ValueError(
                "The dispatcher supports a file-backed SQLite database only: its "
                "single-instance lock is an OS file lock beside the database file. "
                f"DATABASE_URL is {self.database_url!r}."
            )
        return os.path.realpath(os.path.abspath(url.database)) + ".dispatcher.lock"

    @property
    def agent_model_context_headroom_tokens(self) -> int:
        """What the model's own window leaves for input after output and margin."""
        return (
            self.agent_model_context_tokens
            - self.agent_max_output_tokens
            - self.agent_context_safety_margin_tokens
        )

    @property
    def agent_max_input_tokens(self) -> int:
        """Usable input budget: the smaller of the two ceilings above."""
        return min(self.agent_max_context_tokens, self.agent_model_context_headroom_tokens)

    @property
    def agent_context_limit_reason(self) -> str:
        """Which ceiling is binding, so an error message can say so truthfully."""
        if self.agent_max_context_tokens <= self.agent_model_context_headroom_tokens:
            return (
                f"the application limit AGENT_MAX_CONTEXT_TOKENS="
                f"{self.agent_max_context_tokens:,}"
            )
        return (
            f"the model's capacity ({self.agent_model_context_tokens:,} tokens) less "
            f"{self.agent_max_output_tokens:,} reserved for output and "
            f"{self.agent_context_safety_margin_tokens:,} safety margin"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def frozen_execution_config(self) -> dict[str, Any]:
        """The settings every attempt of one orchestration must share.

        Persisted on the orchestration at creation; each child applies them with
        `with_execution_config`, so a changed default or .env between the
        coordinator starting and a child starting cannot make siblings run under
        different budgets. Contains no credentials: the API key is deliberately
        not a frozen field (it is a `SecretStr` and never persisted).
        """
        frozen: dict[str, Any] = {}
        for name in type(self).model_fields:
            if name in FROZEN_EXACT or name.startswith(FROZEN_PREFIXES):
                frozen[name] = getattr(self, name)
        return frozen

    def with_execution_config(self, frozen: dict[str, Any]) -> "Settings":
        """A copy of these settings with an orchestration's frozen values applied."""
        allowed = {
            name
            for name in type(self).model_fields
            if name in FROZEN_EXACT or name.startswith(FROZEN_PREFIXES)
        }
        unknown = set(frozen) - allowed
        if unknown:
            raise ValueError(f"Unexpected frozen settings: {sorted(unknown)}")
        return self.model_copy(update=frozen)

    def subprocess_environment(self, **overrides: str) -> dict[str, str]:
        """The environment for an orchestrator's child process.

        Inherited from this process (so credentials reach the child the same way
        they reached us, never via argv), with explicit overrides — the database
        URL, so a child always writes where its coordinator reads. Lives here
        because this is the only module that reads the environment.
        """
        environment = dict(os.environ)
        environment.update(overrides)
        return environment


# Frozen per orchestration: the model, and every agent, GitHub, and verification
# budget. `anthropic_api_key` is excluded by construction (not in either set).
FROZEN_EXACT = frozenset({"anthropic_model"})
FROZEN_PREFIXES = ("agent_", "github_", "verify_")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
