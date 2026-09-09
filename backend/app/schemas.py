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

from app.models import MAX_PARALLEL_ATTEMPTS, MIN_PARALLEL_ATTEMPTS
from app.time_utils import to_iso_utc
from app.validators import validate_github_repository_url

MAX_ISSUE_DESCRIPTION_LENGTH = 10_000


class RunStatus(str, Enum):
    """Run lifecycle states. Only `pending` is reachable in this milestone."""

    PENDING = "pending"


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


class HealthRead(BaseModel):
    status: str
    service: str
    version: str
