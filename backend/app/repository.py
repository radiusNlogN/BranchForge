"""Database access for runs.

Every SQLAlchemy query lives here. Route handlers stay thin, and a future worker
process can import this module without depending on FastAPI.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RUN_STATUS_PENDING, Run


def create_run(
    db: Session,
    *,
    repository_url: str,
    issue_description: str,
    max_parallel_attempts: int,
) -> Run:
    """Insert a run in the `pending` state and return the persisted row."""
    run = Run(
        repository_url=repository_url,
        issue_description=issue_description,
        max_parallel_attempts=max_parallel_attempts,
        status=RUN_STATUS_PENDING,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def list_runs(db: Session, *, limit: int) -> list[Run]:
    """Return runs newest first, capped at `limit`.

    `id` breaks ties so that runs sharing a timestamp still have a stable order.
    """
    statement = select(Run).order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)
    return list(db.scalars(statement))


def get_run(db: Session, run_id: str) -> Run | None:
    """Return the run with this id, or None if there is no such run."""
    return db.get(Run, run_id)
