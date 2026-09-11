"""Run intake and retrieval endpoints.

Handlers are deliberately thin: validate via schema, delegate to `repository`,
return the persisted row. They are sync `def` because the SQLAlchemy session is
synchronous — FastAPI runs these in a worker thread, so blocking database calls
never stall the event loop.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app import repository
from app.config import settings
from app.database import get_db
from app.schemas import (
    AttemptEventRead,
    InspectionRead,
    OrchestrationRead,
    PatchAttemptRead,
    RunCreate,
    RunDetailRead,
    VerificationRead,
    RunRead,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> RunRead:
    """Persist a new run in the `pending` state.

    The run is stored only. No cloning, analysis, or agent execution happens —
    that is not implemented in this milestone.
    """
    run = repository.create_run(
        db,
        repository_url=payload.repository_url,
        issue_description=payload.issue_description,
        max_parallel_attempts=payload.max_parallel_attempts,
    )
    return RunRead.model_validate(run)


@router.get("", response_model=list[RunRead])
def list_runs(
    db: Session = Depends(get_db),
    limit: int | None = Query(
        default=None,
        ge=1,
        le=settings.run_list_max_limit,
        description="Maximum number of runs to return, newest first.",
    ),
) -> list[RunRead]:
    """Return runs newest first, bounded by `limit`."""
    effective_limit = limit if limit is not None else settings.run_list_default_limit
    runs = repository.list_runs(db, limit=effective_limit)
    return [RunRead.model_validate(run) for run in runs]


@router.get("/{run_id}", response_model=RunDetailRead)
def get_run(
    run_id: str = Path(description="Server-generated run UUID."),
    db: Session = Depends(get_db),
) -> RunDetailRead:
    """Return one run with its inspection, attempts, and orchestration, or 404.

    Each is empty/`null` until the corresponding worker command has run. The
    response stays bounded: at most three attempts, the workers' budgets bound
    what they write, and each attempt's event list is capped here as well.
    """
    run = repository.get_run(db, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run found with id {run_id!r}.",
        )

    # Validated from the plain RunRead, not the ORM row: the row's `attempts` and
    # `orchestration` relationships would otherwise be copied in without events
    # or verifications, and then duplicated below.
    detail = RunDetailRead.model_validate(RunRead.model_validate(run), from_attributes=True)

    inspection = repository.get_inspection_for_run(db, run_id)
    if inspection is not None:
        detail.inspection = InspectionRead.model_validate(inspection)

    orchestration = repository.get_orchestration_for_run(db, run_id)
    if orchestration is not None:
        detail.orchestration = OrchestrationRead.model_validate(orchestration)

    for attempt, verification in repository.list_attempts_with_verifications(db, run_id):
        item = PatchAttemptRead.model_validate(attempt)
        events = repository.list_attempt_events(db, attempt.id, limit=settings.agent_max_events)
        item.events = [AttemptEventRead.model_validate(event) for event in events]
        item.events_total = repository.count_attempt_events(db, attempt.id)
        if verification is not None:
            item.verification = VerificationRead.model_validate(verification)
        detail.attempts.append(item)

    return detail
