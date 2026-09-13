"""Run intake and retrieval endpoints.

Handlers are deliberately thin: validate via schema, delegate to `repository`,
return the persisted row. They are sync `def` because the SQLAlchemy session is
synchronous — FastAPI runs these in a worker thread, so blocking database calls
never stall the event loop.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app import repository
from app.config import settings
from app.database import get_db
from app.models import (
    RUN_STATUS_FAILED,
    RUN_STATUS_INSPECTING,
    RUN_STATUS_PENDING,
    RUN_STATUS_READY,
)
from app.schemas import (
    AttemptEventRead,
    ExecutionJobRead,
    InspectionRead,
    OrchestrationRead,
    PatchAttemptRead,
    RunCreate,
    RunDetailRead,
    RunProgressRead,
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

    job = repository.get_execution_job_for_run(db, run_id)
    if job is not None:
        detail.job = ExecutionJobRead.model_validate(job)

    for attempt, verification in repository.list_attempts_with_verifications(db, run_id):
        item = PatchAttemptRead.model_validate(attempt)
        events = repository.list_attempt_events(db, attempt.id, limit=settings.agent_max_events)
        item.events = [AttemptEventRead.model_validate(event) for event in events]
        item.events_total = repository.count_attempt_events(db, attempt.id)
        if verification is not None:
            item.verification = VerificationRead.model_validate(verification)
        detail.attempts.append(item)

    return detail


@router.get("/{run_id}/progress", response_model=RunProgressRead)
def get_run_progress(
    run_id: str = Path(description="Server-generated run UUID."),
    db: Session = Depends(get_db),
) -> RunProgressRead:
    """Run, job, and per-attempt state — the small response a poll can repeat.

    Deliberately excludes diffs, container logs, events, inspection reports, and
    the comparison. Those belong to `GET /api/runs/{run_id}`, which is fetched
    when something actually changes rather than on every tick.
    """
    progress = repository.run_progress(db, run_id)
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run found with id {run_id!r}.",
        )
    # The job comes back as an ORM row inside a plain dict. Converting it here,
    # rather than letting the parent model infer it, keeps this independent of
    # whether `from_attributes` propagates into a dict-validated parent — a
    # detail that would surface as a runtime error rather than a type error.
    job = progress.pop("job")
    return RunProgressRead.model_validate(
        {
            **progress,
            "job": ExecutionJobRead.model_validate(job) if job is not None else None,
        }
    )


def _start_refusal(db: Session, run: Any) -> str | None:
    """Why this run cannot be started, or None.

    Only two shapes are startable: a `pending` run, which is inspected and then
    orchestrated; and a `ready` run with a usable inspection and no attempts,
    which skips straight to orchestration.
    """
    if run.status == RUN_STATUS_INSPECTING:
        return (
            f"Run {run.id} is being inspected by a worker started outside the queue. "
            f"Wait for it to finish, then start the run."
        )
    if run.status == RUN_STATUS_FAILED:
        return (
            f"Run {run.id}'s inspection failed, so there is nothing to work from. "
            f"Create a new run to try again."
        )
    if repository.get_orchestration_for_run(db, run.id) is not None:
        return f"Run {run.id} has already been orchestrated. One orchestration per run."
    if repository.list_attempts_for_run(db, run.id):
        return (
            f"Run {run.id} already has a patch attempt created with `propose`. Starting "
            f"it would mix manual and orchestrated attempts, so it is refused. Create a "
            f"new run instead."
        )
    if run.status == RUN_STATUS_READY:
        inspection = repository.get_inspection_for_run(db, run.id)
        if inspection is None or not inspection.report or not inspection.commit_sha:
            return (
                f"Run {run.id} is 'ready' but has no usable inspection report to work "
                f"from, so no patch could be proposed against a known commit."
            )
        return None
    if run.status != RUN_STATUS_PENDING:  # pragma: no cover - the enum has four values
        return f"Run {run.id} is {run.status!r} and cannot be started."
    return None


@router.post(
    "/{run_id}/start",
    response_model=ExecutionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_run(
    run_id: str = Path(description="Server-generated run UUID."),
    db: Session = Depends(get_db),
) -> ExecutionJobRead:
    """Enqueue this run's whole workflow and return promptly.

    Accepted, not done: this handler writes one row. It never inspects, calls a
    model, or starts a process — a dispatcher does that, and until one is running
    the job simply waits. Nothing is run in a BackgroundTask either, because work
    that outlives the response would die with the API process.

    Repeated starts return the existing job. That check comes *before*
    eligibility on purpose: once a job has begun, its run is no longer in a
    startable state, so testing eligibility first would answer a second Start
    with a 409 complaining about the very work the caller started.
    """
    run = repository.get_run(db, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run found with id {run_id!r}.",
        )

    existing = repository.get_execution_job_for_run(db, run_id)
    if existing is not None:
        return ExecutionJobRead.model_validate(existing)

    # After the existing-job return, for the same reason as eligibility: a repeat
    # Start must still get its own job back.
    if not settings.dispatcher_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This deployment runs no dispatcher, so a started run would wait in the "
                "queue forever. Runs can be created and viewed here, but not started."
            ),
        )

    refusal = _start_refusal(db, run)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)

    # The INSERT is the claim, so two requests racing through the check above
    # still produce exactly one job.
    return ExecutionJobRead.model_validate(repository.enqueue_execution_job(db, run_id=run_id))


@router.post(
    "/{run_id}/cancel",
    response_model=ExecutionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_run(
    run_id: str = Path(description="Server-generated run UUID."),
    db: Session = Depends(get_db),
) -> ExecutionJobRead:
    """Request cancellation. Idempotent, and never blocked by a stopped daemon.

    A queued job is cancelled outright here: it has no dispatcher, no processes,
    and no containers, so cancelling needs neither a running dispatcher nor a
    working Docker daemon.

    A running job records the request and stays non-terminal until its dispatcher
    has stopped the work and confirmed container cleanup — reporting "cancelled"
    while a container is still running would be a claim we cannot support.

    A job that already finished is returned unchanged: if completion won the race,
    that result stands.
    """
    if repository.get_run(db, run_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No run found with id {run_id!r}.",
        )
    job = repository.request_job_cancellation(db, run_id=run_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} has not been started, so there is nothing to cancel.",
        )
    return ExecutionJobRead.model_validate(job)
