"""SQLAlchemy ORM models."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.time_utils import utcnow

# Run lifecycle. A worker moves pending -> inspecting, then to ready or failed.
# "ready" means repository inspection completed, not that a patch exists.
RUN_STATUS_PENDING = "pending"
RUN_STATUS_INSPECTING = "inspecting"
RUN_STATUS_READY = "ready"
RUN_STATUS_FAILED = "failed"

# Patch-attempt lifecycle. Deliberately SEPARATE from the run status: a run stays
# `ready` (meaning inspection succeeded) whether an attempt succeeds or fails.
VERIFY_STATUS_RUNNING = "running"
VERIFY_STATUS_COMPLETED = "completed"
VERIFY_STATUS_FAILED = "failed"

ATTEMPT_STATUS_RUNNING = "running"
ATTEMPT_STATUS_SUCCEEDED = "succeeded"
ATTEMPT_STATUS_FAILED = "failed"

MIN_PARALLEL_ATTEMPTS = 1
MAX_PARALLEL_ATTEMPTS = 3


class Run(Base):
    """An investigation request: a repository, an issue, and how many attempts to allow.

    A run is created `pending`. The inspection worker claims it into `inspecting`
    and finishes it as `ready` or `failed`. No AI fix attempts or test execution
    exist yet.
    """

    __tablename__ = "runs"
    __table_args__ = (
        # A range invariant worth enforcing in the database too, so a future
        # worker inserting directly cannot violate it.
        CheckConstraint(
            f"max_parallel_attempts >= {MIN_PARALLEL_ATTEMPTS} "
            f"AND max_parallel_attempts <= {MAX_PARALLEL_ATTEMPTS}",
            name="ck_runs_max_parallel_attempts",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    repository_url: Mapped[str] = mapped_column(String(500), nullable=False)
    issue_description: Mapped[str] = mapped_column(Text, nullable=False)
    max_parallel_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=MIN_PARALLEL_ATTEMPTS, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RUN_STATUS_PENDING, server_default=RUN_STATUS_PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    inspection: Mapped["Inspection | None"] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )
    patch_attempt: Mapped["PatchAttempt | None"] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Run id={self.id!r} status={self.status!r} repo={self.repository_url!r}>"


class Inspection(Base):
    """The result of one read-only inspection of a run's repository.

    One row per run in this milestone: only a `pending` run can be claimed, so a
    run is inspected at most once. Columns describing the repository are nullable
    because an early failure means the value was never learned.
    """

    __tablename__ = "inspections"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repository_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repository_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The full structured report. Bounded at write time by the worker's budgets.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tree_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="inspection")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Inspection run_id={self.run_id!r} sha={self.commit_sha!r}>"


class PatchAttempt(Base):
    """One bounded agent attempt to propose a patch for a run.

    `run_id` is UNIQUE, which is what makes creating the row an atomic claim:
    two workers racing to start an attempt cannot both insert, so only one ever
    calls the model. One attempt per inspected run in this milestone.

    The stored diff is a *proposal*. It is never applied and never tested.
    """

    __tablename__ = "patch_attempts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ATTEMPT_STATUS_RUNNING,
        server_default=ATTEMPT_STATUS_RUNNING,
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_test_command: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reported by the provider. Left NULL when unavailable — never fabricated.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="patch_attempt")
    verification: Mapped["Verification | None"] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", uselist=False
    )
    events: Mapped[list["AttemptEvent"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="AttemptEvent.seq",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PatchAttempt run_id={self.run_id!r} status={self.status!r}>"


class AttemptEvent(Base):
    """One ordered operational event during an attempt.

    Records what the controller did — a file was requested, a tool errored, a
    patch was submitted — never the model's hidden reasoning. `detail` is
    truncated at write time so a row cannot grow without bound.
    """

    __tablename__ = "attempt_events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("patch_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    attempt: Mapped["PatchAttempt"] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttemptEvent seq={self.seq} kind={self.kind!r}>"


class Verification(Base):
    """One execution of a proposed patch against the repository's own tests.

    `status` says whether the verification itself ran; `outcome` says what it
    found. They are separate on purpose — "the verification completed" and "the
    patch fixes the bug" are different claims, and collapsing them into one field
    is how a green badge starts meaning nothing.

    One verification per patch attempt: `attempt_id` is UNIQUE, so inserting the
    row *is* the atomic claim.
    """

    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("patch_attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VERIFY_STATUS_RUNNING,
        server_default=VERIFY_STATUS_RUNNING,
    )
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Exactly what was verified, so a result can be reproduced or disbelieved.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    patch_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    runner_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    patch_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    patch_apply_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    patch_touched_tests: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    files_changed: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    baseline_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    patched_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    comparison: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    baseline_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    patched_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Supplemental evidence lives in its own columns. A supplemental failure or
    # timeout must never overwrite the baseline-versus-patched result.
    supplemental_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    supplemental_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempt: Mapped["PatchAttempt"] = relationship(back_populates="verification")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Verification attempt_id={self.attempt_id!r} outcome={self.outcome!r}>"
