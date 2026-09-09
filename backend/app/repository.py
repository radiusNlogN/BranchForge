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
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INSPECTING,
    RUN_STATUS_PENDING,
    RUN_STATUS_READY,
    VERIFY_STATUS_COMPLETED,
    VERIFY_STATUS_FAILED,
    VERIFY_STATUS_RUNNING,
    AttemptEvent,
    Inspection,
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


def get_patch_attempt_for_run(db: Session, run_id: str) -> PatchAttempt | None:
    return db.scalar(select(PatchAttempt).where(PatchAttempt.run_id == run_id))


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
    """Create the attempt row, which *is* the claim.

    `patch_attempts.run_id` is UNIQUE, so a second worker's INSERT violates the
    constraint and this returns None — meaning that caller must not call the
    model. Committed before any network work begins.
    """
    attempt = PatchAttempt(
        run_id=run_id,
        status=ATTEMPT_STATUS_RUNNING,
        model=model,
        commit_sha=commit_sha,
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


def get_verification_for_run(db: Session, run_id: str) -> Verification | None:
    """Fetch the verification belonging to a run's patch attempt, if any."""
    return db.execute(
        select(Verification)
        .join(PatchAttempt, Verification.attempt_id == PatchAttempt.id)
        .where(PatchAttempt.run_id == run_id)
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
