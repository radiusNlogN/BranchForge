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
