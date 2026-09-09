"""SQLAlchemy ORM models."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.time_utils import utcnow

RUN_STATUS_PENDING = "pending"
MIN_PARALLEL_ATTEMPTS = 1
MAX_PARALLEL_ATTEMPTS = 3


class Run(Base):
    """An investigation request: a repository, an issue, and how many attempts to allow.

    A run is created in the `pending` state. Nothing in this milestone advances it —
    execution is not implemented yet.
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

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Run id={self.id!r} status={self.status!r} repo={self.repository_url!r}>"
