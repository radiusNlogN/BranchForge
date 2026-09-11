"""Pydantic request and response schemas — the API contract."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
)

from app.models import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_INTERRUPTED,
    ATTEMPT_STATUS_QUEUED,
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    MAX_PARALLEL_ATTEMPTS,
    MIN_PARALLEL_ATTEMPTS,
    ORCH_STATUS_COMPLETED,
    ORCH_STATUS_FAILED,
    ORCH_STATUS_INTERRUPTED,
    ORCH_STATUS_QUEUED,
    ORCH_STATUS_RUNNING,
    RUN_STATUS_FAILED,
    RUN_STATUS_INSPECTING,
    RUN_STATUS_PENDING,
    RUN_STATUS_READY,
    VERIFY_STATUS_COMPLETED,
    VERIFY_STATUS_FAILED,
    VERIFY_STATUS_INTERRUPTED,
    VERIFY_STATUS_RUNNING,
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


# --- Patch attempt ----------------------------------------------------------


class AttemptStatus(str, Enum):
    """Patch-attempt (proposal) states, independent of the run's own status."""

    QUEUED = ATTEMPT_STATUS_QUEUED
    RUNNING = ATTEMPT_STATUS_RUNNING
    SUCCEEDED = ATTEMPT_STATUS_SUCCEEDED
    FAILED = ATTEMPT_STATUS_FAILED
    INTERRUPTED = ATTEMPT_STATUS_INTERRUPTED


class AttemptEventRead(BaseModel):
    """One operational event. Never contains model reasoning."""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    summary: str
    detail: str | None
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_timestamp(self, value: datetime) -> str:
        return to_iso_utc(value)


class PatchAttemptRead(BaseModel):
    """A persisted patch attempt.

    The diff is an UNVERIFIED proposal: it was never applied and no tests were
    run. `events` is capped; `events_total` reports how many exist.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    attempt_index: int
    orchestration_id: str | None
    emphasis_key: str | None
    emphasis_text: str | None
    status: AttemptStatus
    model: str
    commit_sha: str | None
    diff: str | None
    summary: str | None
    suggested_test_command: str | None
    input_tokens: int | None
    output_tokens: int | None
    error_kind: str | None
    error_message: str | None
    pipeline_error_kind: str | None
    pipeline_error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None

    events: list[AttemptEventRead] = Field(default_factory=list)
    events_total: int = 0
    verification: "VerificationRead | None" = None

    @field_serializer("started_at", "completed_at")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return to_iso_utc(value) if value is not None else None


# --- Verification -----------------------------------------------------------


class VerificationStatus(str, Enum):
    """Did the verification run? Separate from what it found."""

    RUNNING = VERIFY_STATUS_RUNNING
    COMPLETED = VERIFY_STATUS_COMPLETED
    FAILED = VERIFY_STATUS_FAILED
    INTERRUPTED = VERIFY_STATUS_INTERRUPTED


class VerificationRead(BaseModel):
    """A persisted verification.

    `status` reports whether the verification executed; `outcome` reports what it
    found. Both are needed: a completed verification whose outcome is
    `still_failing` is a successful verification of an unsuccessful patch.

    The claim is narrow by construction — it covers the tests that actually ran,
    on one commit, in one runner profile.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    attempt_id: str
    status: VerificationStatus
    outcome: str | None
    detail: str | None

    commit_sha: str | None
    patch_sha256: str | None
    profile: str | None
    image_ref: str | None
    image_id: str | None
    runner_args: list[str] | None = None

    patch_applied: bool
    patch_apply_message: str | None
    patch_touched_tests: bool
    files_changed: list[str] | None = None

    baseline_summary: dict[str, Any] | None = None
    patched_summary: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    baseline_log: str | None = None
    patched_log: str | None = None

    supplemental_summary: dict[str, Any] | None = None
    supplemental_log: str | None = None

    notes: list[str] | None = None
    error_kind: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None

    @field_serializer("started_at", "completed_at")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return to_iso_utc(value) if value is not None else None


class OrchestrationStatus(str, Enum):
    """Did the orchestration run to the end? Separate from what its attempts found."""

    QUEUED = ORCH_STATUS_QUEUED
    RUNNING = ORCH_STATUS_RUNNING
    COMPLETED = ORCH_STATUS_COMPLETED
    INTERRUPTED = ORCH_STATUS_INTERRUPTED
    FAILED = ORCH_STATUS_FAILED


class OrchestrationRead(BaseModel):
    """A persisted orchestration and its comparison.

    `comparison` is written once, at the end. `recommended_attempt_index` is NULL
    whenever no attempt demonstrated a fix — the least-bad patch is never chosen.
    `execution_config` is omitted: it is operational detail, not evidence.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    status: OrchestrationStatus
    requested_attempts: int
    concurrency_limit: int
    effective_concurrency: int
    model: str
    commit_sha: str
    profile: str
    image_ref: str
    image_id: str
    recommended_attempt_index: int | None
    comparison: dict[str, Any] | None = None
    notes: list[str] | None = None
    error_kind: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @field_serializer("created_at", "started_at", "completed_at")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return to_iso_utc(value) if value is not None else None


class RunDetailRead(RunRead):
    """A run plus its inspection, every attempt (each with its verification),
    and the orchestration if there is one.

    The list endpoint deliberately returns `RunRead` without these — they would
    make a list response unbounded. Legacy (manual) runs have one attempt and no
    orchestration.
    """

    inspection: InspectionRead | None = None
    orchestration: OrchestrationRead | None = None
    attempts: list[PatchAttemptRead] = Field(default_factory=list)


PatchAttemptRead.model_rebuild()


class HealthRead(BaseModel):
    status: str
    service: str
    version: str
