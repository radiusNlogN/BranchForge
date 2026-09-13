"""Starting and cancelling a run over HTTP.

Every test here is about the endpoint's contract, not about execution. The
handlers write one row and return, so nothing in this file starts a process,
calls a model, or needs Docker — and if one of these tests ever did need those,
that would itself be the bug.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import agent, repository, worker
from app.config import settings
from app.models import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    PatchAttempt,
)
from tests.conftest import COMMIT_SHA, INSPECTABLE_PAYLOAD, FakeGitHub


def make_run(client, **overrides) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, **overrides)
    return client.post("/api/runs", json=payload).json()["id"]


def inspected_run(client, session_factory, worker_settings, **overrides) -> str:
    """A run whose inspection really ran, against the mocked GitHub transport."""
    run_id = make_run(client, **overrides)
    assert (
        worker.inspect_run(
            run_id,
            session_factory=session_factory,
            client_factory=FakeGitHub().client_factory,
            config=worker_settings,
            out=io.StringIO(),
        )
        == worker.EXIT_OK
    )
    return run_id


def independent_session_factory(database_url: str):
    """A session factory with its own engine, for genuine cross-process contention."""
    engine = create_engine(
        database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# --- Starting -----------------------------------------------------------------


def test_starting_a_pending_run_queues_a_job(client) -> None:
    run_id = make_run(client)
    response = client.post(f"/api/runs/{run_id}/start")

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == run_id
    assert body["status"] == JOB_STATUS_QUEUED
    assert body["stage"] is None
    assert body["cancel_requested"] is False
    assert body["recovery_required"] is False
    # Accepted, not done: the run is untouched until a dispatcher picks it up.
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "pending"


def test_starting_is_refused_on_a_deployment_without_a_dispatcher(client, db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "dispatcher_available", False)
    run_id = make_run(client)
    response = client.post(f"/api/runs/{run_id}/start")

    assert response.status_code == 409
    assert "no dispatcher" in response.json()["detail"]
    # Refused means nothing was queued: no job exists to wait forever.
    assert db.execute(text("SELECT COUNT(*) FROM execution_jobs")).scalar() == 0
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "pending"


def test_an_existing_job_is_still_returned_without_a_dispatcher(client, monkeypatch) -> None:
    """The existing-job return precedes the refusal, like it precedes eligibility."""
    run_id = make_run(client)
    first = client.post(f"/api/runs/{run_id}/start").json()

    monkeypatch.setattr(settings, "dispatcher_available", False)
    again = client.post(f"/api/runs/{run_id}/start")

    assert again.status_code == 202
    assert again.json()["id"] == first["id"]


def test_starting_an_inspected_run_is_allowed(client, session_factory, worker_settings) -> None:
    """A `ready` run skips inspection; the dispatcher goes straight to orchestrating."""
    run_id = inspected_run(client, session_factory, worker_settings)
    response = client.post(f"/api/runs/{run_id}/start")

    assert response.status_code == 202
    assert response.json()["status"] == JOB_STATUS_QUEUED
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "ready"


def test_repeated_starts_return_the_same_job_and_create_no_duplicate(client, db) -> None:
    run_id = make_run(client)
    first = client.post(f"/api/runs/{run_id}/start").json()
    second = client.post(f"/api/runs/{run_id}/start").json()
    third = client.post(f"/api/runs/{run_id}/start").json()

    assert first["id"] == second["id"] == third["id"]
    assert db.execute(text("SELECT COUNT(*) FROM execution_jobs")).scalar() == 1


def test_repeated_starts_keep_working_once_the_job_is_running(client, db) -> None:
    """Idempotency is checked before eligibility, on purpose.

    Once a job is running, its run is no longer in a startable state. If the
    handler tested eligibility first, a second Start would be answered with a 409
    complaining about the very work the caller had started.
    """
    run_id = make_run(client)
    first = client.post(f"/api/runs/{run_id}/start").json()
    assert repository.claim_queued_job(db, job_id=first["id"], dispatcher_id="d1")

    again = client.post(f"/api/runs/{run_id}/start")
    assert again.status_code == 202
    assert again.json()["id"] == first["id"]
    assert again.json()["status"] == JOB_STATUS_RUNNING


def test_repeated_start_after_completion_returns_the_finished_job(client, db) -> None:
    run_id = make_run(client)
    job = client.post(f"/api/runs/{run_id}/start").json()
    assert repository.claim_queued_job(db, job_id=job["id"], dispatcher_id="d1")
    assert repository.finish_job(db, job_id=job["id"], status=JOB_STATUS_COMPLETED)

    again = client.post(f"/api/runs/{run_id}/start")
    assert again.status_code == 202
    assert again.json()["id"] == job["id"]
    assert again.json()["status"] == JOB_STATUS_COMPLETED


def test_concurrent_starts_enqueue_exactly_one_job(client, database_url, db) -> None:
    """Two connections race to start one run; the UNIQUE run_id settles it.

    Independent engines, released together by a barrier — a check-then-insert
    would produce two rows under this test.
    """
    run_id = make_run(client)
    factories = [independent_session_factory(database_url) for _ in range(4)]
    barrier = threading.Barrier(len(factories))

    def start(factory) -> str:
        barrier.wait(timeout=10)
        with factory() as session:
            return repository.enqueue_execution_job(session, run_id=run_id).id

    with ThreadPoolExecutor(max_workers=len(factories)) as pool:
        job_ids = list(pool.map(start, factories))

    assert len(set(job_ids)) == 1, f"expected one job, got {set(job_ids)}"
    assert db.execute(text("SELECT COUNT(*) FROM execution_jobs")).scalar() == 1


def test_starting_an_unknown_run_is_404(client) -> None:
    response = client.post("/api/runs/00000000-0000-0000-0000-000000000000/start")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_starting_a_run_with_a_manual_attempt_is_refused(
    client, session_factory, worker_settings, db
) -> None:
    run_id = inspected_run(client, session_factory, worker_settings)
    db.add(PatchAttempt(run_id=run_id, attempt_index=1, model="m", commit_sha=COMMIT_SHA))
    db.commit()

    response = client.post(f"/api/runs/{run_id}/start")
    assert response.status_code == 409
    assert "propose" in response.json()["detail"]
    assert repository.get_execution_job_for_run(db, run_id) is None


def test_starting_an_already_orchestrated_run_is_refused(
    client, session_factory, worker_settings, db
) -> None:
    run_id = inspected_run(client, session_factory, worker_settings)
    claimed = repository.claim_orchestration(
        db,
        run_id=run_id,
        requested_attempts=1,
        concurrency_limit=1,
        effective_concurrency=1,
        model="m",
        commit_sha=COMMIT_SHA,
        profile="python-pytest",
        image_ref="img:1",
        image_id="sha256:abc",
        workspace_root="/tmp/ws",
        execution_config={},
        emphases=[agent.emphasis_for(1)],
    )
    assert claimed is not None

    response = client.post(f"/api/runs/{run_id}/start")
    assert response.status_code == 409
    assert "already been orchestrated" in response.json()["detail"]


def test_starting_a_failed_run_is_refused(client, db) -> None:
    run_id = make_run(client)
    repository.save_inspection_failure(
        db,
        run_id=run_id,
        error_kind="not_found",
        error_message="no such repository",
        started_at=repository.utcnow(),
    )

    response = client.post(f"/api/runs/{run_id}/start")
    assert response.status_code == 409
    assert "failed" in response.json()["detail"]


def test_starting_a_run_being_inspected_is_refused(client, db) -> None:
    run_id = make_run(client)
    assert repository.claim_run_for_inspection(db, run_id)

    response = client.post(f"/api/runs/{run_id}/start")
    assert response.status_code == 409
    assert "inspected" in response.json()["detail"]


def test_starting_a_ready_run_without_a_commit_sha_is_refused(
    client, session_factory, worker_settings, db
) -> None:
    """Caught here rather than failing the job later for a knowable reason."""
    run_id = inspected_run(client, session_factory, worker_settings)
    inspection = repository.get_inspection_for_run(db, run_id)
    inspection.commit_sha = None
    db.commit()

    response = client.post(f"/api/runs/{run_id}/start")
    assert response.status_code == 409
    assert "usable inspection" in response.json()["detail"]


# --- Cancelling ----------------------------------------------------------------


def test_cancelling_a_queued_job_cancels_it_outright(client, db) -> None:
    """No dispatcher, no Docker, no processes — so there is nothing to wind down."""
    run_id = make_run(client)
    client.post(f"/api/runs/{run_id}/start")

    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == JOB_STATUS_CANCELLED
    assert body["cancel_requested"] is True
    assert body["completed_at"] is not None


def test_cancelling_a_running_job_records_the_request_but_leaves_it_running(client, db) -> None:
    """It stays non-terminal until its work has stopped and cleanup is confirmed."""
    run_id = make_run(client)
    job = client.post(f"/api/runs/{run_id}/start").json()
    assert repository.claim_queued_job(db, job_id=job["id"], dispatcher_id="d1")

    body = client.post(f"/api/runs/{run_id}/cancel").json()
    assert body["status"] == JOB_STATUS_RUNNING
    assert body["cancel_requested"] is True
    assert body["completed_at"] is None


def test_cancelling_is_idempotent(client, db) -> None:
    run_id = make_run(client)
    client.post(f"/api/runs/{run_id}/start")
    first = client.post(f"/api/runs/{run_id}/cancel").json()
    second = client.post(f"/api/runs/{run_id}/cancel").json()
    assert first["status"] == second["status"] == JOB_STATUS_CANCELLED
    assert first["id"] == second["id"]


def test_cancelling_a_completed_job_returns_it_unchanged(client, db) -> None:
    """The completion race: if completion committed first, that result stands."""
    run_id = make_run(client)
    job = client.post(f"/api/runs/{run_id}/start").json()
    assert repository.claim_queued_job(db, job_id=job["id"], dispatcher_id="d1")
    assert repository.finish_job(db, job_id=job["id"], status=JOB_STATUS_COMPLETED)

    body = client.post(f"/api/runs/{run_id}/cancel").json()
    assert body["status"] == JOB_STATUS_COMPLETED
    assert body["cancel_requested"] is False


def test_cancelling_a_run_that_was_never_started_is_404(client) -> None:
    run_id = make_run(client)
    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 404
    assert "not been started" in response.json()["detail"]


def test_cancelling_an_unknown_run_is_404(client) -> None:
    response = client.post("/api/runs/00000000-0000-0000-0000-000000000000/cancel")
    assert response.status_code == 404


def test_a_cancelled_queued_job_can_never_be_claimed(client, db) -> None:
    """Cancellation beats launch: the claim's guard is what makes that true."""
    run_id = make_run(client)
    job = client.post(f"/api/runs/{run_id}/start").json()
    client.post(f"/api/runs/{run_id}/cancel")

    assert not repository.claim_queued_job(db, job_id=job["id"], dispatcher_id="d1")


def test_cancel_versus_claim_has_exactly_one_winner(client, database_url, db) -> None:
    """Either cancellation prevents the launch, or the claim wins and the
    dispatcher will observe the request while the job runs. Never both, never
    a launched job that is also already terminal."""
    run_id = make_run(client)
    job_id = client.post(f"/api/runs/{run_id}/start").json()["id"]

    cancel_factory = independent_session_factory(database_url)
    claim_factory = independent_session_factory(database_url)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def cancel() -> None:
        barrier.wait(timeout=10)
        with cancel_factory() as session:
            repository.request_job_cancellation(session, run_id=run_id)

    def claim() -> None:
        barrier.wait(timeout=10)
        with claim_factory() as session:
            results["claimed"] = repository.claim_queued_job(
                session, job_id=job_id, dispatcher_id="d1"
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda fn: fn(), [cancel, claim]))

    db.expire_all()
    job = repository.get_execution_job(db, job_id)
    assert job.cancel_requested is True
    if results["claimed"]:
        # The claim won: the job is running and the dispatcher must act on the
        # request. It is emphatically not terminal yet.
        assert job.status == JOB_STATUS_RUNNING
        assert job.completed_at is None
    else:
        # Cancellation won: nothing was ever launched.
        assert job.status == JOB_STATUS_CANCELLED


# --- The detail view -----------------------------------------------------------


def test_the_detail_endpoint_includes_the_job(client) -> None:
    """So Start/Cancel render from the persisted record on first load."""
    run_id = make_run(client)
    assert client.get(f"/api/runs/{run_id}").json()["job"] is None

    client.post(f"/api/runs/{run_id}/start")
    job = client.get(f"/api/runs/{run_id}").json()["job"]
    assert job is not None and job["status"] == JOB_STATUS_QUEUED
