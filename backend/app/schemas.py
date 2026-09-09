"""Pydantic request and response schemas — the API contract."""

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
)

from app.models import (
    MAX_PARALLEL_ATTEMPTS,
    MIN_PARALLEL_ATTEMPTS,
    RUN_STATUS_FAILED,
    RUN_STATUS_INSPECTING,
    RUN_STATUS_PENDING,
    RUN_STATUS_READY,
)
from app.time_utils import to_iso_utc
from app.validators import validate_github_repository_url

MAX_ISSUE_DESCRIPTION_LENGTH = 10_000


class RunStatus(str, Enum):
    """Run lifecycle states.

    `ready` means repository inspection finished — not that a fix or patch exists.
    """

    PENDING = RUN_STATUS_PENDING
    INSPECTING = RUN_STATUS_INSPECTING
    READY = RUN_STATUS_READY
    FAILED = RUN_STATUS_FAILED


def _clean_issue_description(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Issue description must not be empty or whitespace-only.")
    return cleaned


RepositoryUrl = Annotated[str, AfterValidator(validate_github_repository_url)]

IssueDescription = Annotated[
    str,
    StringConstraints(max_length=MAX_ISSUE_DESCRIPTION_LENGTH),
    AfterValidator(_clean_issue_description),
]


class RunCreate(BaseModel):
    """Body of POST /api/runs. The server assigns id, status, and timestamps."""

    model_config = ConfigDict(extra="forbid")

    repository_url: RepositoryUrl = Field(
        description="HTTPS URL of a public GitHub repository, e.g. https://github.com/octocat/Hello-World",
    )
    issue_description: IssueDescription = Field(
        description="What should be investigated. Must be non-empty after trimming.",
    )
    max_parallel_attempts: int = Field(
        default=MIN_PARALLEL_ATTEMPTS,
        ge=MIN_PARALLEL_ATTEMPTS,
        le=MAX_PARALLEL_ATTEMPTS,
        description="How many competing fix attempts to allow, 1-3.",
    )


class RunRead(BaseModel):
    """A persisted run, exactly as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    repository_url: str
    issue_description: str
    max_parallel_attempts: int
    status: RunStatus
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        # SQLite can hand back naive datetimes; every stored value is UTC, so
        # awareness is restored here and the wire format always carries a zone.
        return to_iso_utc(value)


# --- Inspection report ------------------------------------------------------
#
# These models are the shape of the JSON stored in `inspections.report` *and* the
# shape returned by the API, so the two cannot drift apart.


class RepositoryFacts(BaseModel):
    """Facts read from GitHub, all pinned to one commit."""

    full_name: str | None = None
    name: str | None = None
    description: str | None = None
    default_branch: str | None = None
    commit_sha: str | None = None
    is_private: bool | None = None
    is_fork: bool | None = None
    is_archived: bool | None = None


class FileEntry(BaseModel):
    path: str
    size: int | None = None


class FilePreview(BaseModel):
    """One selected file. Untrusted repository text, for display only."""

    path: str
    size: int | None = None
    content: str | None = None
    bytes_shown: int = 0
    content_truncated: bool = False
    omitted: bool = False
    omitted_reason: str | None = None
    is_binary: bool = False


class PytestAssessment(BaseModel):
    """A filename-based heuristic, with the evidence that produced it."""

    is_python_project: bool
    uses_pytest: bool
    python_evidence: list[str] = Field(default_factory=list)
    pytest_evidence: list[str] = Field(default_factory=list)
    test_locations: list[str] = Field(default_factory=list)
    caveat: str


class BudgetUsage(BaseModel):
    requests_made: int
    max_requests: int
    files_in_tree: int
    files_listed: int
    max_files_listed: int
    files_fetched: int
    max_files_fetched: int
    content_bytes_stored: int
    max_total_content_bytes: int
    max_file_bytes: int


class TruncationInfo(BaseModel):
    tree_truncated: bool = False
    file_listing_truncated: bool = False
    omitted_files: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class InspectionReport(BaseModel):
    repository: RepositoryFacts
    files: list[FileEntry] = Field(default_factory=list)
    readme: FilePreview | None = None
    python_config_files: list[FilePreview] = Field(default_factory=list)
    assessment: PytestAssessment
    budgets: BudgetUsage
    truncation: TruncationInfo


class InspectionRead(BaseModel):
    """A persisted inspection, successful or failed."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    commit_sha: str | None
    default_branch: str | None
    repository_name: str | None
    repository_description: str | None
    tree_truncated: bool
    report: InspectionReport | None
    started_at: datetime
    completed_at: datetime | None
    error_kind: str | None
    error_message: str | None

    @field_serializer("started_at", "completed_at")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return to_iso_utc(value) if value is not None else None


class RunDetailRead(RunRead):
    """A run plus its inspection, for the detail view.

    The list endpoint deliberately returns `RunRead` without this — inspections
    would make a list response unbounded.
    """

    inspection: InspectionRead | None = None


class HealthRead(BaseModel):
    status: str
    service: str
    version: str
