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
    UniqueConstraint,
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

# Verification lifecycle: did the verification itself run? Separate from what it
# found (`outcome`). `interrupted` is written only when an orchestrator stopped
# the child that was running it.
VERIFY_STATUS_RUNNING = "running"
VERIFY_STATUS_COMPLETED = "completed"
VERIFY_STATUS_FAILED = "failed"
VERIFY_STATUS_INTERRUPTED = "interrupted"

# Patch-attempt lifecycle. Deliberately SEPARATE from the run status: a run stays
# `ready` (meaning inspection succeeded) whether an attempt succeeds or fails.
# It describes the *proposal* only. `queued` exists for orchestrator-reserved
# slots that no child has claimed yet.
ATTEMPT_STATUS_QUEUED = "queued"
ATTEMPT_STATUS_RUNNING = "running"
ATTEMPT_STATUS_SUCCEEDED = "succeeded"
ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_INTERRUPTED = "interrupted"

# Orchestration lifecycle. `failed` means the coordinator itself failed (or could
# not confirm container cleanup) — not that attempts failed; attempt outcomes
# live on the attempts.
ORCH_STATUS_QUEUED = "queued"
ORCH_STATUS_RUNNING = "running"
ORCH_STATUS_COMPLETED = "completed"
ORCH_STATUS_INTERRUPTED = "interrupted"
ORCH_STATUS_FAILED = "failed"

# Execution-job lifecycle: did the *dispatcher* carry this run's workflow through?
# Deliberately separate from every status above — a job can be cancelled while the
# inspection, attempts, and verifications it already produced stay exactly as they
# were recorded. `cancelled` means a person asked for it to stop; `interrupted`
# means the dispatcher itself shut down. Keeping those apart is the whole point:
# one is a decision, the other is an operational event.
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"

JOB_TERMINAL_STATUSES = (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_INTERRUPTED,
)

# Which child the dispatcher is running. Progress detail, never a status.
JOB_STAGE_INSPECTING = "inspecting"
JOB_STAGE_ORCHESTRATING = "orchestrating"

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
    attempts: Mapped[list["PatchAttempt"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PatchAttempt.attempt_index",
    )
    orchestration: Mapped["Orchestration | None"] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )
    job: Mapped["ExecutionJob | None"] = relationship(
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


class Orchestration(Base):
    """One run of competing attempts for a run.

    `run_id` is UNIQUE: inserting this row, together with its reserved attempt
    slots in the same commit, *is* the claim. Two orchestrators targeting one run
    cannot both succeed, so the loser launches nothing.

    Everything a child needs to behave identically to its siblings is frozen here
    at creation: the model, the effective agent/verification budgets
    (`execution_config`), the resolved image ID, and the workspace root. Children
    read these from this row rather than re-reading possibly changed defaults.
    """

    __tablename__ = "orchestrations"
    __table_args__ = (
        CheckConstraint(
            f"requested_attempts >= {MIN_PARALLEL_ATTEMPTS} "
            f"AND requested_attempts <= {MAX_PARALLEL_ATTEMPTS}",
            name="ck_orchestrations_requested_attempts",
        ),
    )

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
        String(20), nullable=False, default=ORCH_STATUS_QUEUED, server_default=ORCH_STATUS_QUEUED
    )

    requested_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)

    model: Mapped[str] = mapped_column(String(100), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    profile: Mapped[str] = mapped_column(String(40), nullable=False)
    image_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    image_id: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_root: Mapped[str] = mapped_column(Text, nullable=False)
    # Frozen settings overrides; contains no credentials (see config.frozen_execution_config).
    execution_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    recommended_attempt_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comparison: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="orchestration")
    attempts: Mapped[list["PatchAttempt"]] = relationship(
        viewonly=True, order_by="PatchAttempt.attempt_index"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Orchestration run_id={self.run_id!r} status={self.status!r}>"


class ExecutionJob(Base):
    """One request to run a run's whole workflow: inspect, then orchestrate.

    `run_id` is UNIQUE, so inserting this row *is* the enqueue claim. Repeated
    `POST /start` calls collapse onto the one job and two racing requests cannot
    create duplicate work — the same mechanism as `orchestrations.run_id`. Because
    retries are out of scope, a run is started at most once.

    The job's status describes the *dispatcher's* handling of the run and nothing
    else. It never rewrites what the inspector, the agents, or the verifier
    recorded: a cancelled job leaves every artifact already produced exactly as it
    was. `stage` is progress detail, not a status.

    `cancel_requested` is a request, not a state. A queued job can go straight to
    `cancelled`, but an active one stays non-terminal until its children have
    actually stopped and container cleanup has been confirmed — otherwise the UI
    would report a cancellation that had not happened yet.
    """

    __tablename__ = "execution_jobs"

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
        String(20), nullable=False, default=JOB_STATUS_QUEUED, server_default=JOB_STATUS_QUEUED
    )
    stage: Mapped[str | None] = mapped_column(String(30), nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Which dispatcher last owned this job. Informational only: exclusivity is
    # enforced by the OS-level dispatcher lock, never by trusting this column.
    dispatcher_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # True when this job was found `running` with no dispatcher owning it — an
    # abrupt crash. Its children and containers may still be alive, so nothing is
    # replayed and no further job is launched until a person has cleaned up.
    recovery_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Bounded operator notes. Appended de-duplicated, so repeated dispatcher
    # restarts cannot grow this row without limit.
    notes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="job")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ExecutionJob run_id={self.run_id!r} status={self.status!r} stage={self.stage!r}>"


class PatchAttempt(Base):
    """One bounded agent attempt to propose a patch for a run.

    `(run_id, attempt_index)` is UNIQUE. A manual `propose` inserts index 1 and
    that insert is its claim; an orchestrator reserves indices 1..N as `queued`
    rows in the same commit as its orchestration row, and a child then claims one
    by ID with a conditional UPDATE. Index 1 being contested by both paths is
    what makes a manual attempt and an orchestration mutually exclusive.

    The stored diff is a *proposal*. `status` describes only the proposal;
    `pipeline_error_*` records an orchestrated pipeline that stopped before its
    verification finished (e.g. the child exited between the two steps).
    """

    __tablename__ = "patch_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_index", name="uq_patch_attempts_run_attempt_index"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    orchestration_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("orchestrations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # The small, recorded difference in investigation emphasis. NULL for manual attempts.
    emphasis_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    emphasis_text: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # Written only by orchestrator reconciliation; never changes `status`.
    pipeline_error_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pipeline_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The OS process id of the child running this attempt's pipeline, recorded by
    # the coordinator at spawn and cleared when it exits.
    #
    # This exists because an attempt child is started with `start_new_session=True`
    # and therefore leads its OWN session: signalling the coordinator's process
    # group provably cannot reach it. If a dispatcher has to force-kill a
    # coordinator, this column is the only record of what that coordinator had
    # running, and without it those children would keep calling the model and
    # starting containers. A pid alone is not proof of identity — pids are reused —
    # so a signaller must confirm the process is still this attempt's worker before
    # signalling it.
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # NULL while queued: a reserved slot has not started. No default on purpose —
    # SQLAlchemy would fire it for an explicit None, giving queued rows a start
    # time. Claims set it explicitly.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="attempts")
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
