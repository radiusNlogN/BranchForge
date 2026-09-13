"""Database access for runs.

Every SQLAlchemy query lives here. Route handlers stay thin, and a future worker
process can import this module without depending on FastAPI.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_INTERRUPTED,
    ATTEMPT_STATUS_QUEUED,
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
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
    AttemptEvent,
    ExecutionJob,
    Inspection,
    Orchestration,
    PatchAttempt,
    Run,
    Verification,
)
from app.time_utils import utcnow


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


def get_inspection_for_run(db: Session, run_id: str) -> Inspection | None:
    """Return the inspection belonging to this run, if one exists."""
    return db.scalar(select(Inspection).where(Inspection.run_id == run_id))


def claim_run_for_inspection(db: Session, run_id: str) -> bool:
    """Atomically move a run from `pending` to `inspecting`.

    The status guard lives in the UPDATE's WHERE clause, so two workers racing for
    the same run cannot both succeed: exactly one UPDATE matches a row. Returns
    True if this caller won the claim.

    The claim is committed before the caller does any network work, and this
    session must be closed before that work begins — a transaction is never held
    open across HTTP requests.
    """
    result = db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == RUN_STATUS_PENDING)
        .values(status=RUN_STATUS_INSPECTING, updated_at=utcnow())
    )
    db.commit()
    return bool(result.rowcount == 1)


def save_inspection_success(
    db: Session,
    *,
    run_id: str,
    commit_sha: str,
    default_branch: str,
    repository_name: str | None,
    repository_description: str | None,
    tree_truncated: bool,
    report: dict[str, Any],
    started_at: datetime,
) -> Inspection:
    """Store the report and mark the run ready — in one transaction.

    Both changes share a single commit so a run can never be `ready` without its
    report, or carry a report while still looking unfinished.
    """
    inspection = Inspection(
        run_id=run_id,
        commit_sha=commit_sha,
        default_branch=default_branch,
        repository_name=repository_name,
        repository_description=repository_description,
        report=report,
        tree_truncated=tree_truncated,
        started_at=started_at,
        completed_at=utcnow(),
    )
    db.add(inspection)
    db.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(status=RUN_STATUS_READY, updated_at=utcnow())
    )
    db.commit()
    db.refresh(inspection)
    return inspection


def save_inspection_failure(
    db: Session,
    *,
    run_id: str,
    error_kind: str,
    error_message: str,
    started_at: datetime,
    commit_sha: str | None = None,
    default_branch: str | None = None,
) -> Inspection:
    """Record why an inspection failed and mark the run failed — one transaction."""
    inspection = Inspection(
        run_id=run_id,
        commit_sha=commit_sha,
        default_branch=default_branch,
        started_at=started_at,
        completed_at=utcnow(),
        error_kind=error_kind,
        error_message=error_message,
    )
    db.add(inspection)
    db.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(status=RUN_STATUS_FAILED, updated_at=utcnow())
    )
    db.commit()
    db.refresh(inspection)
    return inspection


# --- Patch attempts ---------------------------------------------------------
#
# Note on run status: none of these functions touch `Run.status`. A run stays
# `ready` — meaning its inspection succeeded — whether an attempt succeeds or
# fails. Attempt state is tracked only on the attempt.


class AmbiguousAttempt(Exception):
    """A run-level lookup found several attempts; the caller must name one."""

    def __init__(self, attempt_ids: list[str]) -> None:
        super().__init__(f"{len(attempt_ids)} attempts")
        self.attempt_ids = attempt_ids


def list_attempts_for_run(db: Session, run_id: str) -> list[PatchAttempt]:
    return list(
        db.scalars(
            select(PatchAttempt)
            .where(PatchAttempt.run_id == run_id)
            .order_by(PatchAttempt.attempt_index)
        )
    )


def get_patch_attempt(db: Session, attempt_id: str) -> PatchAttempt | None:
    return db.get(PatchAttempt, attempt_id)


def get_patch_attempt_for_run(db: Session, run_id: str) -> PatchAttempt | None:
    """The run's only attempt, for single-attempt callers.

    Raises `AmbiguousAttempt` when there are several — a run-level question has
    no single answer then, and guessing would verify the wrong patch.
    """
    attempts = list_attempts_for_run(db, run_id)
    if len(attempts) > 1:
        raise AmbiguousAttempt([a.id for a in attempts])
    return attempts[0] if attempts else None


def list_attempt_events(db: Session, attempt_id: str, *, limit: int) -> list[AttemptEvent]:
    """Events in order, capped."""
    statement = (
        select(AttemptEvent)
        .where(AttemptEvent.attempt_id == attempt_id)
        .order_by(AttemptEvent.seq)
        .limit(limit)
    )
    return list(db.scalars(statement))


def count_attempt_events(db: Session, attempt_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(AttemptEvent)
            .where(AttemptEvent.attempt_id == attempt_id)
        )
        or 0
    )


def claim_patch_attempt(
    db: Session, *, run_id: str, model: str, commit_sha: str | None
) -> PatchAttempt | None:
    """Create a manual attempt (index 1), which *is* the claim.

    `(run_id, attempt_index)` is UNIQUE and a manual attempt is always index 1,
    so a second worker's INSERT — or an orchestrator reserving slot 1 at the same
    moment — violates the constraint and this returns None, meaning that caller
    must not call the model. Committed before any network work begins.
    """
    attempt = PatchAttempt(
        run_id=run_id,
        attempt_index=1,
        orchestration_id=None,
        status=ATTEMPT_STATUS_RUNNING,
        model=model,
        commit_sha=commit_sha,
        started_at=utcnow(),
    )
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(attempt)
    return attempt


def record_attempt_event(
    db: Session,
    *,
    attempt_id: str,
    kind: str,
    summary: str,
    detail: str | None,
    max_events: int,
    max_detail_chars: int,
) -> None:
    """Append one operational event in its own short transaction.

    Committed immediately so the dashboard's Refresh shows progress while the
    attempt is still running. Silently stops appending once `max_events` is
    reached, so a long attempt cannot grow the table without bound.
    """
    existing = count_attempt_events(db, attempt_id)
    if existing >= max_events:
        return

    db.add(
        AttemptEvent(
            attempt_id=attempt_id,
            seq=existing + 1,
            kind=kind[:40],
            summary=summary[:max_detail_chars],
            detail=detail[:max_detail_chars] if detail else None,
        )
    )
    db.commit()


def save_attempt_success(
    db: Session,
    *,
    attempt_id: str,
    diff: str,
    summary: str,
    suggested_test_command: str,
    input_tokens: int | None,
    output_tokens: int | None,
    max_summary_chars: int,
    max_command_chars: int,
) -> PatchAttempt:
    """Store the patch and mark the attempt succeeded in one transaction."""
    attempt = db.get(PatchAttempt, attempt_id)
    if attempt is None:  # pragma: no cover - the caller just created it
        raise ValueError(f"No patch attempt {attempt_id!r}.")

    attempt.diff = diff
    attempt.summary = summary[:max_summary_chars]
    attempt.suggested_test_command = suggested_test_command[:max_command_chars]
    attempt.input_tokens = input_tokens
    attempt.output_tokens = output_tokens
    attempt.status = ATTEMPT_STATUS_SUCCEEDED
    attempt.completed_at = utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt


def save_attempt_failure(
    db: Session,
    *,
    attempt_id: str,
    error_kind: str,
    error_message: str,
    input_tokens: int | None,
    output_tokens: int | None,
    max_error_chars: int,
) -> PatchAttempt:
    """Record why the attempt failed, in one transaction."""
    attempt = db.get(PatchAttempt, attempt_id)
    if attempt is None:  # pragma: no cover
        raise ValueError(f"No patch attempt {attempt_id!r}.")

    attempt.status = ATTEMPT_STATUS_FAILED
    attempt.error_kind = error_kind[:50]
    attempt.error_message = error_message[:max_error_chars]
    attempt.input_tokens = input_tokens
    attempt.output_tokens = output_tokens
    attempt.completed_at = utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt


# --- Verification (milestone 4) ---------------------------------------------
# None of these functions touch Run.status or PatchAttempt.status. A run stays
# `ready` and an attempt stays `succeeded` no matter what a verification finds:
# those statuses describe inspection and proposal, not whether the patch works.


def get_verification_for_attempt(db: Session, attempt_id: str) -> Verification | None:
    return db.execute(
        select(Verification).where(Verification.attempt_id == attempt_id)
    ).scalar_one_or_none()


def claim_verification(
    db: Session,
    *,
    attempt_id: str,
    commit_sha: str | None,
    patch_sha256: str,
    profile: str,
    image_ref: str,
    image_id: str,
) -> Verification | None:
    """Atomically claim the one verification slot for this attempt.

    `verifications.attempt_id` is UNIQUE, so the INSERT itself is the claim: a
    second worker gets an IntegrityError and returns None having started no
    container and downloaded no source. Committed before any expensive work.
    """
    verification = Verification(
        attempt_id=attempt_id,
        status=VERIFY_STATUS_RUNNING,
        commit_sha=commit_sha,
        patch_sha256=patch_sha256,
        profile=profile,
        image_ref=image_ref,
        image_id=image_id,
    )
    db.add(verification)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(verification)
    return verification


def save_verification_result(
    db: Session,
    *,
    verification_id: str,
    outcome: str,
    detail: str,
    patch_applied: bool,
    patch_apply_message: str,
    patch_touched_tests: bool,
    files_changed: list[str],
    runner_args: list[str],
    baseline_summary: dict[str, Any] | None,
    patched_summary: dict[str, Any] | None,
    comparison: dict[str, Any] | None,
    baseline_log: str,
    patched_log: str,
    supplemental_summary: dict[str, Any] | None,
    supplemental_log: str,
    notes: list[str],
    max_log_chars: int,
    max_error_chars: int,
) -> None:
    """Store a completed verification: outcome and evidence in one commit."""
    verification = db.get(Verification, verification_id)
    if verification is None:  # pragma: no cover - defensive
        return

    verification.status = VERIFY_STATUS_COMPLETED
    verification.outcome = outcome
    verification.detail = detail[:max_error_chars]
    verification.patch_applied = patch_applied
    verification.patch_apply_message = patch_apply_message[:max_error_chars]
    verification.patch_touched_tests = patch_touched_tests
    verification.files_changed = files_changed
    verification.runner_args = runner_args
    verification.baseline_summary = baseline_summary
    verification.patched_summary = patched_summary
    verification.comparison = comparison
    verification.baseline_log = baseline_log[:max_log_chars]
    verification.patched_log = patched_log[:max_log_chars]
    verification.supplemental_summary = supplemental_summary
    verification.supplemental_log = supplemental_log[:max_log_chars]
    verification.notes = notes
    verification.completed_at = utcnow()
    db.commit()


def save_verification_failure(
    db: Session,
    *,
    verification_id: str,
    error_kind: str,
    error_message: str,
    max_error_chars: int,
) -> None:
    """Record that the verification itself could not run."""
    verification = db.get(Verification, verification_id)
    if verification is None:  # pragma: no cover - defensive
        return

    verification.status = VERIFY_STATUS_FAILED
    verification.error_kind = error_kind[:50]
    verification.error_message = error_message[:max_error_chars]
    verification.completed_at = utcnow()
    db.commit()


# --- Orchestration (milestone 5) --------------------------------------------
# Same discipline as everything above: short transactions, conditional UPDATEs
# for every state change that could race, and nothing here writes `Run.status`.
# Reconciliation writes `PatchAttempt.status` only while the *proposal* is still
# unfinished (queued/running); a finished proposal's status is never changed, and
# an unfinished verification is recorded on the verification and as a pipeline
# error on the attempt instead.


def get_orchestration(db: Session, orchestration_id: str) -> Orchestration | None:
    return db.get(Orchestration, orchestration_id)


def get_orchestration_for_run(db: Session, run_id: str) -> Orchestration | None:
    return db.scalar(select(Orchestration).where(Orchestration.run_id == run_id))


def claim_orchestration(
    db: Session,
    *,
    run_id: str,
    requested_attempts: int,
    concurrency_limit: int,
    effective_concurrency: int,
    model: str,
    commit_sha: str,
    profile: str,
    image_ref: str,
    image_id: str,
    workspace_root: str,
    execution_config: dict[str, Any],
    emphases: list[tuple[str, str]],
) -> Orchestration | None:
    """Insert the orchestration AND reserve every attempt slot in one commit.

    That single commit is the claim. `orchestrations.run_id` is UNIQUE, so a
    second orchestrator's insert fails; `(run_id, attempt_index)` is UNIQUE, so a
    manual attempt already holding index 1 (or racing for it) fails the reservation
    too. Either way this returns None with nothing written, and the caller must
    launch nothing.
    """
    if len(emphases) != requested_attempts:  # pragma: no cover - caller bug
        raise ValueError("One emphasis per attempt is required.")

    orchestration = Orchestration(
        run_id=run_id,
        status=ORCH_STATUS_QUEUED,
        requested_attempts=requested_attempts,
        concurrency_limit=concurrency_limit,
        effective_concurrency=effective_concurrency,
        model=model,
        commit_sha=commit_sha,
        profile=profile,
        image_ref=image_ref,
        image_id=image_id,
        workspace_root=workspace_root,
        execution_config=execution_config,
    )
    db.add(orchestration)
    try:
        db.flush()
        for index, (key, text) in enumerate(emphases, start=1):
            db.add(
                PatchAttempt(
                    run_id=run_id,
                    orchestration_id=orchestration.id,
                    attempt_index=index,
                    status=ATTEMPT_STATUS_QUEUED,
                    model=model,
                    commit_sha=commit_sha,
                    emphasis_key=key,
                    emphasis_text=text,
                    started_at=None,
                )
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(orchestration)
    return orchestration


def claim_queued_attempt(
    db: Session, *, attempt_id: str, orchestration_id: str, model: str
) -> bool:
    """Atomically move one reserved slot from `queued` to `running`.

    The attempt must belong to `orchestration_id` — a child's arguments are
    checked against the database, not trusted. `rowcount == 1` means this caller
    won; anything else means another process claimed it, it was interrupted, or
    it is not this orchestration's.
    """
    result = db.execute(
        update(PatchAttempt)
        .where(
            PatchAttempt.id == attempt_id,
            PatchAttempt.orchestration_id == orchestration_id,
            PatchAttempt.status == ATTEMPT_STATUS_QUEUED,
        )
        .values(status=ATTEMPT_STATUS_RUNNING, model=model, started_at=utcnow())
    )
    db.commit()
    return bool(result.rowcount == 1)


def start_orchestration(db: Session, orchestration_id: str) -> None:
    db.execute(
        update(Orchestration)
        .where(
            Orchestration.id == orchestration_id,
            Orchestration.status == ORCH_STATUS_QUEUED,
        )
        .values(status=ORCH_STATUS_RUNNING, started_at=utcnow())
    )
    db.commit()


# Pipeline error kinds, written by reconciliation only.
PIPELINE_WORKER_EXITED = "worker_exited"
PIPELINE_VERIFICATION_NOT_STARTED = "verification_not_started"
PIPELINE_VERIFICATION_UNFINISHED = "verification_unfinished"
PIPELINE_INTERRUPTED = "interrupted"
PIPELINE_NOT_LAUNCHED = "not_launched"


def reconcile_attempt_pipeline(
    db: Session,
    *,
    attempt_id: str,
    interrupted: bool,
    exit_description: str,
    max_error_chars: int,
) -> str:
    """After a child exits — for ANY reason, exit code 0 included — make the
    database say what actually happened to its pipeline.

    Completion is decided from persisted state, never from the exit code: a
    child can exit 0 having saved a patch but never started its verification.
    Returns one of "complete", "proposal_unfinished", "verification_not_started",
    "verification_unfinished". All writes are conditional and share one commit,
    so a result written just before the child exited is never overwritten.
    """
    attempt = db.get(PatchAttempt, attempt_id)
    if attempt is None:  # pragma: no cover - the coordinator reserved it
        return "complete"
    now = utcnow()

    if attempt.status in (ATTEMPT_STATUS_QUEUED, ATTEMPT_STATUS_RUNNING):
        if interrupted:
            status, kind = ATTEMPT_STATUS_INTERRUPTED, PIPELINE_INTERRUPTED
            message = f"The orchestration was interrupted before the proposal finished ({exit_description})."
        else:
            status, kind = ATTEMPT_STATUS_FAILED, PIPELINE_WORKER_EXITED
            message = f"The attempt's worker process exited before the proposal finished ({exit_description})."
        db.execute(
            update(PatchAttempt)
            .where(
                PatchAttempt.id == attempt_id,
                PatchAttempt.status.in_([ATTEMPT_STATUS_QUEUED, ATTEMPT_STATUS_RUNNING]),
            )
            .values(
                status=status,
                error_kind=kind,
                error_message=message[:max_error_chars],
                pipeline_error_kind=kind,
                pipeline_error_message=message[:max_error_chars],
                completed_at=now,
            )
        )
        db.commit()
        return "proposal_unfinished"

    # A proposal that produced no diff needs no verification: the pipeline ended.
    if attempt.status != ATTEMPT_STATUS_SUCCEEDED or not attempt.diff:
        return "complete"

    verification = db.scalar(select(Verification).where(Verification.attempt_id == attempt_id))
    if verification is None:
        kind = PIPELINE_INTERRUPTED if interrupted else PIPELINE_VERIFICATION_NOT_STARTED
        message = (
            f"The patch was proposed and saved, but its verification never started "
            f"({exit_description})."
            + (" The orchestration was interrupted." if interrupted else "")
        )
        _set_pipeline_error(db, attempt_id, kind, message[:max_error_chars])
        db.commit()
        return "verification_not_started"

    if verification.status == VERIFY_STATUS_RUNNING:
        if interrupted:
            status, kind = VERIFY_STATUS_INTERRUPTED, PIPELINE_INTERRUPTED
            message = f"The orchestration was interrupted during verification ({exit_description})."
        else:
            status, kind = VERIFY_STATUS_FAILED, PIPELINE_WORKER_EXITED
            message = f"The attempt's worker process exited during verification ({exit_description})."
        db.execute(
            update(Verification)
            .where(
                Verification.id == verification.id,
                Verification.status == VERIFY_STATUS_RUNNING,
            )
            .values(
                status=status,
                error_kind=kind,
                error_message=message[:max_error_chars],
                completed_at=now,
            )
        )
        _set_pipeline_error(
            db, attempt_id, PIPELINE_VERIFICATION_UNFINISHED, message[:max_error_chars]
        )
        db.commit()
        return "verification_unfinished"

    return "complete"


def _set_pipeline_error(db: Session, attempt_id: str, kind: str, message: str) -> None:
    """Record a pipeline error once; never touches the proposal's status."""
    db.execute(
        update(PatchAttempt)
        .where(PatchAttempt.id == attempt_id, PatchAttempt.pipeline_error_kind.is_(None))
        .values(pipeline_error_kind=kind[:50], pipeline_error_message=message)
    )


def mark_unlaunched_attempts(
    db: Session, *, orchestration_id: str, reason: str, max_error_chars: int
) -> int:
    """Queued slots that will never be launched become `interrupted`."""
    result = db.execute(
        update(PatchAttempt)
        .where(
            PatchAttempt.orchestration_id == orchestration_id,
            PatchAttempt.status == ATTEMPT_STATUS_QUEUED,
        )
        .values(
            status=ATTEMPT_STATUS_INTERRUPTED,
            error_kind=PIPELINE_NOT_LAUNCHED,
            error_message=reason[:max_error_chars],
            pipeline_error_kind=PIPELINE_NOT_LAUNCHED,
            pipeline_error_message=reason[:max_error_chars],
            completed_at=utcnow(),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def list_attempts_with_verifications(
    db: Session, run_id: str
) -> list[tuple[PatchAttempt, Verification | None]]:
    attempts = list_attempts_for_run(db, run_id)
    return [(a, get_verification_for_attempt(db, a.id)) for a in attempts]


def finish_orchestration(
    db: Session,
    *,
    orchestration_id: str,
    status: str,
    comparison: dict[str, Any] | None,
    recommended_attempt_index: int | None,
    notes: list[str],
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    orchestration = db.get(Orchestration, orchestration_id)
    if orchestration is None:  # pragma: no cover - defensive
        return
    orchestration.status = status
    orchestration.comparison = comparison
    orchestration.recommended_attempt_index = recommended_attempt_index
    orchestration.notes = notes
    orchestration.error_kind = error_kind[:50] if error_kind else None
    orchestration.error_message = error_message
    orchestration.completed_at = utcnow()
    db.commit()


def reconcile_orchestration(
    db: Session,
    *,
    orchestration_id: str,
    status: str,
    comparison: dict[str, Any] | None,
    recommended_attempt_index: int | None,
    notes: list[str],
    error_kind: str | None = None,
    error_message: str | None = None,
) -> bool:
    """`finish_orchestration`, but only while the orchestration is still active.

    Used when a dispatcher had to force-kill a coordinator and must write the
    ending the coordinator never got to write. The status guard is the whole
    point: a coordinator that saved its comparison microseconds before the
    SIGKILL landed has already recorded the truth, and overwriting it with our
    reconstruction would replace real evidence with a guess. Returns True when
    this call is what finished it.
    """
    result = db.execute(
        update(Orchestration)
        .where(
            Orchestration.id == orchestration_id,
            Orchestration.status.in_([ORCH_STATUS_QUEUED, ORCH_STATUS_RUNNING]),
        )
        .values(
            status=status,
            comparison=comparison,
            recommended_attempt_index=recommended_attempt_index,
            notes=notes,
            error_kind=error_kind[:50] if error_kind else None,
            error_message=error_message,
            completed_at=utcnow(),
        )
    )
    db.commit()
    return bool(result.rowcount == 1)


def record_attempt_worker_pid(
    db: Session, *, attempt_id: str, worker_pid: int | None
) -> None:
    """Record (or clear) the pid of the child running this attempt's pipeline.

    Written by the coordinator at spawn and cleared once the child has been
    reaped. It exists so that a dispatcher forced to SIGKILL a coordinator can
    still find that coordinator's children: each leads its own session, so
    signalling the coordinator's process group cannot reach them.
    """
    db.execute(
        update(PatchAttempt)
        .where(PatchAttempt.id == attempt_id)
        .values(worker_pid=worker_pid)
    )
    db.commit()


def list_attempt_worker_pids(
    db: Session, *, orchestration_id: str
) -> list[tuple[str, int, int]]:
    """(attempt_id, attempt_index, worker_pid) for children still recorded as live.

    A recorded pid is a lead, not proof: pids are reused, so a caller must confirm
    the process really is this attempt's worker before signalling it.
    """
    rows = db.execute(
        select(PatchAttempt.id, PatchAttempt.attempt_index, PatchAttempt.worker_pid)
        .where(
            PatchAttempt.orchestration_id == orchestration_id,
            PatchAttempt.worker_pid.is_not(None),
        )
        .order_by(PatchAttempt.attempt_index)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


# --- Execution jobs (milestone 6) -------------------------------------------
#
# The queue a dispatcher drains. Same discipline as everything above: short
# transactions, and a conditional UPDATE for every state change that could race.
#
# One rule matters more than the rest, because it is what keeps cancellation
# honest: nothing in this section writes `PatchAttempt.status`, any verification
# field, or a comparison. A job's lifecycle describes the *dispatcher's handling*
# of a run and nothing else, so cancelling a job never rewrites, hides, or
# invalidates the inspection, patches, and test results already produced. They
# stay exactly as they were recorded; the job simply stops.
#
# `reconcile_inspection` below is the one place outside inspection that writes
# `Run.status`. It is the same class of write as `reconcile_attempt_pipeline`
# finishing an unfinished proposal: a run left `inspecting` by a child that died
# is not a run being inspected, and leaving it that way would claim work is in
# progress when nothing is running.

JOB_ERROR_RUN_STATE_CHANGED = "run_state_changed"
JOB_ERROR_DOCKER_UNAVAILABLE = "docker_unavailable"
JOB_ERROR_MODEL_NOT_CONFIGURED = "model_not_configured"
JOB_ERROR_INSPECTION_FAILED = "inspection_failed"
JOB_ERROR_ORCHESTRATION_FAILED = "orchestration_failed"
JOB_ERROR_DISPATCHER_STOPPED = "dispatcher_stopped"
JOB_ERROR_CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


def get_execution_job_for_run(db: Session, run_id: str) -> ExecutionJob | None:
    return db.scalar(select(ExecutionJob).where(ExecutionJob.run_id == run_id))


def get_execution_job(db: Session, job_id: str) -> ExecutionJob | None:
    return db.get(ExecutionJob, job_id)


def enqueue_execution_job(db: Session, *, run_id: str) -> ExecutionJob:
    """Enqueue the one job for this run, or return the job that already exists.

    `execution_jobs.run_id` is UNIQUE, so the INSERT *is* the claim: two requests
    racing to start the same run cannot create two jobs, and the loser reads back
    the winner's row rather than failing. Repeated Start requests are therefore
    idempotent by construction rather than by a check-then-insert that has a gap
    between the two halves.
    """
    job = ExecutionJob(run_id=run_id, status=JOB_STATUS_QUEUED)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = get_execution_job_for_run(db, run_id)
        if existing is None:  # pragma: no cover - the constraint implies one exists
            raise
        return existing
    db.refresh(job)
    return job


def request_job_cancellation(db: Session, *, run_id: str) -> ExecutionJob | None:
    """Record a cancellation request. Idempotent; terminal jobs are untouched.

    A *queued* job is cancelled outright here and now. It has no dispatcher, no
    child processes, and no containers, so there is nothing to wind down and
    nothing to confirm — which is why cancelling one needs neither a running
    dispatcher nor a working Docker daemon.

    A *running* job only records the request. It stays non-terminal until its
    children have actually stopped and container cleanup has been confirmed,
    because reporting "cancelled" while a container is still running would be a
    claim we cannot back up.

    A job that already finished is left exactly as it is: if the completion was
    committed first, that result stands and this returns it unchanged.
    """
    job = get_execution_job_for_run(db, run_id)
    if job is None:
        return None

    now = utcnow()
    cancelled_outright = db.execute(
        update(ExecutionJob)
        .where(ExecutionJob.id == job.id, ExecutionJob.status == JOB_STATUS_QUEUED)
        .values(
            status=JOB_STATUS_CANCELLED,
            cancel_requested=True,
            cancel_requested_at=now,
            completed_at=now,
        )
    )
    if cancelled_outright.rowcount != 1:
        db.execute(
            update(ExecutionJob)
            .where(ExecutionJob.id == job.id, ExecutionJob.status == JOB_STATUS_RUNNING)
            .values(cancel_requested=True, cancel_requested_at=now)
        )
    db.commit()
    db.refresh(job)
    return job


def next_queued_job(db: Session) -> ExecutionJob | None:
    """The oldest queued job, or None. A peek — claiming is a separate step."""
    return db.scalar(
        select(ExecutionJob)
        .where(ExecutionJob.status == JOB_STATUS_QUEUED)
        .order_by(ExecutionJob.created_at, ExecutionJob.id)
        .limit(1)
    )


def claim_queued_job(db: Session, *, job_id: str, dispatcher_id: str) -> bool:
    """Atomically move one queued job to `running`. `rowcount == 1` means we won.

    The `cancel_requested` guard is what makes "cancel beats launch" a real
    property rather than a hope: a job cancelled between the peek and the claim
    fails this UPDATE, so no child is ever started for it.
    """
    result = db.execute(
        update(ExecutionJob)
        .where(
            ExecutionJob.id == job_id,
            ExecutionJob.status == JOB_STATUS_QUEUED,
            ExecutionJob.cancel_requested.is_(False),
        )
        .values(status=JOB_STATUS_RUNNING, dispatcher_id=dispatcher_id, started_at=utcnow())
    )
    db.commit()
    return bool(result.rowcount == 1)


def cancel_unlaunched_job(db: Session, *, job_id: str) -> bool:
    """Finish a still-queued job as `cancelled` without ever launching it."""
    now = utcnow()
    result = db.execute(
        update(ExecutionJob)
        .where(ExecutionJob.id == job_id, ExecutionJob.status == JOB_STATUS_QUEUED)
        .values(status=JOB_STATUS_CANCELLED, cancel_requested=True, completed_at=now)
    )
    db.commit()
    return bool(result.rowcount == 1)


def set_job_stage(db: Session, *, job_id: str, stage: str | None) -> None:
    """Record which child the dispatcher is running. Progress detail, not status."""
    db.execute(
        update(ExecutionJob)
        .where(ExecutionJob.id == job_id, ExecutionJob.status == JOB_STATUS_RUNNING)
        .values(stage=stage)
    )
    db.commit()


def job_cancel_requested(db: Session, *, job_id: str) -> bool:
    """Cheap poll of one flag while a child runs."""
    return bool(
        db.scalar(select(ExecutionJob.cancel_requested).where(ExecutionJob.id == job_id))
    )


def finish_job(
    db: Session,
    *,
    job_id: str,
    status: str,
    error_kind: str | None = None,
    error_message: str | None = None,
    max_error_chars: int = 2_000,
    note: str | None = None,
    max_notes: int = 20,
) -> bool:
    """Finish an active job. Guarded, so a terminal job is never re-finished."""
    job = db.get(ExecutionJob, job_id)
    if job is None:  # pragma: no cover - defensive
        return False
    notes = _append_job_note(job.notes, note, limit=max_notes) if note else job.notes
    result = db.execute(
        update(ExecutionJob)
        .where(
            ExecutionJob.id == job_id,
            ExecutionJob.status.in_([JOB_STATUS_QUEUED, JOB_STATUS_RUNNING]),
        )
        .values(
            status=status,
            stage=None,
            notes=notes,
            error_kind=error_kind[:50] if error_kind else None,
            error_message=error_message[:max_error_chars] if error_message else None,
            completed_at=utcnow(),
        )
    )
    db.commit()
    return bool(result.rowcount == 1)


def _append_job_note(existing: list[str] | None, note: str, *, limit: int) -> list[str]:
    """Append a note unless it is already there, keeping at most `limit`.

    De-duplication is what stops a dispatcher that is restarted repeatedly against
    an unrecovered job from appending the same sentence forever.
    """
    notes = list(existing or [])
    if note in notes:
        return notes
    notes.append(note)
    return notes[-limit:]


def list_running_jobs(db: Session) -> list[ExecutionJob]:
    """Jobs recorded as `running`. At dispatcher startup these are crash debris."""
    return list(
        db.scalars(
            select(ExecutionJob)
            .where(ExecutionJob.status == JOB_STATUS_RUNNING)
            .order_by(ExecutionJob.created_at, ExecutionJob.id)
        )
    )


def mark_job_recovery_required(
    db: Session, *, job_id: str, note: str, max_notes: int = 20
) -> bool:
    """Flag a `running` job as needing manual recovery. Never changes its status.

    Holding the dispatcher lock proves no *dispatcher* is running, but it proves
    nothing about the child processes and containers the dead one started. So the
    job is not declared failed, cancelled, or finished — declaring it stopped
    would be a claim we cannot support — and it is not replayed. It is flagged,
    annotated once, and left for a person.
    """
    job = db.get(ExecutionJob, job_id)
    if job is None:  # pragma: no cover - defensive
        return False
    result = db.execute(
        update(ExecutionJob)
        .where(ExecutionJob.id == job_id, ExecutionJob.status == JOB_STATUS_RUNNING)
        .values(
            recovery_required=True,
            notes=_append_job_note(job.notes, note, limit=max_notes),
        )
    )
    db.commit()
    return bool(result.rowcount == 1)


def count_jobs_requiring_recovery(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ExecutionJob)
            .where(ExecutionJob.recovery_required.is_(True))
        )
        or 0
    )


def reconcile_inspection(
    db: Session,
    *,
    run_id: str,
    error_kind: str,
    error_message: str,
    max_error_chars: int,
) -> bool:
    """A run left `inspecting` by a child that died becomes `failed`, with a reason.

    The status change and the recorded reason share one commit, exactly as
    `save_inspection_failure` does, so a run can never be `failed` without saying
    why. Guarded on `inspecting`, so an inspector that finished just before its
    process was killed keeps its real result.
    """
    result = db.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == RUN_STATUS_INSPECTING)
        .values(status=RUN_STATUS_FAILED, updated_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        return False

    now = utcnow()
    inspection = db.scalar(select(Inspection).where(Inspection.run_id == run_id))
    if inspection is None:
        db.add(
            Inspection(
                run_id=run_id,
                started_at=now,
                completed_at=now,
                error_kind=error_kind[:50],
                error_message=error_message[:max_error_chars],
            )
        )
    elif inspection.error_kind is None:
        inspection.error_kind = error_kind[:50]
        inspection.error_message = error_message[:max_error_chars]
        inspection.completed_at = now
    db.commit()
    return True


def run_progress(db: Session, run_id: str) -> dict[str, Any] | None:
    """Everything a poll needs about one run, and nothing more.

    Individual columns are selected rather than whole ORM rows so the expensive
    ones — diffs, container logs, inspection reports, comparisons, event bodies —
    are never loaded, let alone serialized. A run with three attempts has a detail
    payload measured in megabytes; this stays a few hundred bytes, which is what
    makes polling every couple of seconds reasonable.
    """
    run_row = db.execute(
        select(Run.id, Run.status, Run.updated_at).where(Run.id == run_id)
    ).first()
    if run_row is None:
        return None

    job = db.scalar(select(ExecutionJob).where(ExecutionJob.run_id == run_id))

    orchestration_row = db.execute(
        select(
            Orchestration.status,
            Orchestration.recommended_attempt_index,
            Orchestration.started_at,
            Orchestration.completed_at,
        ).where(Orchestration.run_id == run_id)
    ).first()

    attempt_rows = db.execute(
        select(
            PatchAttempt.id,
            PatchAttempt.attempt_index,
            PatchAttempt.status,
            PatchAttempt.error_kind,
            PatchAttempt.pipeline_error_kind,
            PatchAttempt.started_at,
            PatchAttempt.completed_at,
        )
        .where(PatchAttempt.run_id == run_id)
        .order_by(PatchAttempt.attempt_index)
    ).all()
    attempt_ids = [row[0] for row in attempt_rows]

    verifications: dict[str, tuple[str, str | None]] = {}
    event_counts: dict[str, int] = {}
    if attempt_ids:
        for attempt_id, status, outcome in db.execute(
            select(Verification.attempt_id, Verification.status, Verification.outcome).where(
                Verification.attempt_id.in_(attempt_ids)
            )
        ).all():
            verifications[attempt_id] = (status, outcome)
        for attempt_id, count in db.execute(
            select(AttemptEvent.attempt_id, func.count())
            .where(AttemptEvent.attempt_id.in_(attempt_ids))
            .group_by(AttemptEvent.attempt_id)
        ).all():
            event_counts[attempt_id] = int(count)

    attempts: list[dict[str, Any]] = []
    for attempt_id, index, status, error_kind, pipeline_error_kind, started, completed in attempt_rows:
        record = verifications.get(attempt_id)
        attempts.append(
            {
                "attempt_index": index,
                "status": status,
                "error_kind": error_kind,
                "pipeline_error_kind": pipeline_error_kind,
                "events_total": event_counts.get(attempt_id, 0),
                "started_at": started,
                "completed_at": completed,
                "verification": (
                    {"status": record[0], "outcome": record[1]} if record is not None else None
                ),
            }
        )

    return {
        "id": run_row[0],
        "status": run_row[1],
        "updated_at": run_row[2],
        "job": job,
        "orchestration": (
            {
                "status": orchestration_row[0],
                "recommended_attempt_index": orchestration_row[1],
                "started_at": orchestration_row[2],
                "completed_at": orchestration_row[3],
            }
            if orchestration_row is not None
            else None
        ),
        "attempts": attempts,
    }
