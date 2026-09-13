"""The dispatcher, driven as a REAL process.

Children are the real `inspect` code path and the real orchestrator, with the
model, GitHub, and the container runner scripted at the usual seams. The
dispatcher's own behaviour — the OS lock, claiming, staging, cancellation,
reconciliation from the database, orphan reaping, and the container sweep — is
the production code path, unchanged.

Nothing here contacts a network, spends API credit, or needs a Docker daemon: a
fake `docker` script records what is asked of it.

Conditions are waited for by *observing state* (a file the child wrote, a row in
the database), never by sleeping for a guessed interval.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import repository, worker
from app.models import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    ORCH_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_READY,
)
from tests.conftest import INSPECTABLE_PAYLOAD, FakeGitHub

BACKEND_ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = "sha256:testimage"


@pytest.fixture
def fake_docker(tmp_path) -> dict:
    """A `docker` stand-in: logs every call; `ps` prints whatever `ps_output` holds."""
    log = tmp_path / "docker.log"
    ps_output = tmp_path / "docker_ps_output"
    ps_output.write_text("")
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f'if [ "$1" = "ps" ]; then cat "{ps_output}"; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return {"binary": str(script), "log": log, "ps_output": ps_output}


def write_scenario(tmp_path, attempts: dict, **extra) -> tuple[Path, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {"workdir": str(workdir), "image_id": IMAGE_ID, "attempts": attempts, **extra}
        )
    )
    return scenario, workdir


def make_run(client, **overrides) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, **overrides)
    return client.post("/api/runs", json=payload).json()["id"]


def inspect_now(run_id, session_factory, worker_settings) -> None:
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


def enqueue(session_factory, run_id: str) -> str:
    with session_factory() as session:
        return repository.enqueue_execution_job(session, run_id=run_id).id


def start_dispatcher(tmp_path, database_url, scenario, fake_docker, *, name="dispatcher", **flags):
    log_path = tmp_path / f"{name}.log"
    handle = open(log_path, "w")
    argv = [
        sys.executable, "-m", "tests.dispatcher_driver",
        "--scenario", str(scenario),
        "--database-url", database_url,
        "--docker-binary", fake_docker["binary"],
    ]
    for key, value in flags.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    process = subprocess.Popen(argv, cwd=BACKEND_ROOT, stdout=handle, stderr=subprocess.STDOUT)
    return process, handle, log_path


def wait_for(predicate, *, timeout=60.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def job_of(session_factory, run_id: str):
    with session_factory() as session:
        return repository.get_execution_job_for_run(session, run_id)


def stop(process, handle) -> int | None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)
    handle.close()
    return process.returncode


# --- Exclusive ownership ---------------------------------------------------------


def test_a_second_dispatcher_cannot_start_against_the_same_database(
    client, session_factory, database_url, tmp_path, fake_docker
) -> None:
    """The lock is an OS file lock, so the loser does no work at all."""
    scenario, workdir = write_scenario(tmp_path, {"1": {}})
    first, first_log, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: "holding" in (tmp_path / "dispatcher.log").read_text(),
            what="the first dispatcher to take the lock",
        )
        second, second_handle, second_path = start_dispatcher(
            tmp_path, database_url, scenario, fake_docker, name="second"
        )
        assert second.wait(timeout=60) == worker.EXIT_DISPATCHER_LOCKED
        second_handle.close()
        output = second_path.read_text()
        assert "Another dispatcher already holds" in output
        assert "Nothing was claimed" in output
    finally:
        stop(first, first_log)


def test_the_lock_is_released_when_a_dispatcher_exits(
    client, session_factory, database_url, tmp_path, fake_docker
) -> None:
    scenario, _ = write_scenario(tmp_path, {"1": {}})
    first, first_log, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    wait_for(
        lambda: "holding" in (tmp_path / "dispatcher.log").read_text(),
        what="the first dispatcher to take the lock",
    )
    stop(first, first_log)

    second, second_handle, second_path = start_dispatcher(
        tmp_path, database_url, scenario, fake_docker, name="second"
    )
    try:
        wait_for(
            lambda: "holding" in second_path.read_text(),
            what="the second dispatcher to take the freed lock",
        )
    finally:
        stop(second, second_handle)


# --- Staging ----------------------------------------------------------------------


def test_a_pending_run_is_inspected_before_it_is_orchestrated(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    run_id = make_run(client, max_parallel_attempts=1)
    enqueue(session_factory, run_id)
    scenario, workdir = write_scenario(tmp_path, {"1": {"patch": "correct"}})

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: (job_of(session_factory, run_id).status == JOB_STATUS_COMPLETED),
            what="the job to complete",
            timeout=120,
        )
    finally:
        stop(process, handle)

    assert (workdir / "inspect-started").exists(), "the inspection stage must have run"
    with session_factory() as session:
        run = repository.get_run(session, run_id)
        orchestration = repository.get_orchestration_for_run(session, run_id)
        assert run.status == RUN_STATUS_READY
        assert orchestration is not None
        assert orchestration.status == ORCH_STATUS_COMPLETED
        # The inspection finished before any attempt started.
        attempts = repository.list_attempts_for_run(session, run_id)
        assert attempts and attempts[0].commit_sha == orchestration.commit_sha


def test_an_already_inspected_run_skips_inspection(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    run_id = make_run(client, max_parallel_attempts=1)
    inspect_now(run_id, session_factory, worker_settings)
    enqueue(session_factory, run_id)
    scenario, workdir = write_scenario(tmp_path, {"1": {"patch": "correct"}})

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: (job_of(session_factory, run_id).status == JOB_STATUS_COMPLETED),
            what="the job to complete",
            timeout=120,
        )
    finally:
        stop(process, handle)

    assert not (workdir / "inspect-started").exists(), "a ready run must not be re-inspected"


def test_a_failed_inspection_prevents_any_model_execution(
    client, session_factory, database_url, tmp_path, fake_docker
) -> None:
    """No orchestration, no attempt, no prompt: the model is never reached."""
    run_id = make_run(client, max_parallel_attempts=2)
    enqueue(session_factory, run_id)
    scenario, workdir = write_scenario(
        tmp_path, {"1": {}, "2": {}}, inspect={"behaviour": "fail"}
    )

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: (job_of(session_factory, run_id).status == JOB_STATUS_FAILED),
            what="the job to fail",
            timeout=120,
        )
    finally:
        stop(process, handle)

    job = job_of(session_factory, run_id)
    assert job.error_kind == repository.JOB_ERROR_INSPECTION_FAILED
    with session_factory() as session:
        assert repository.get_run(session, run_id).status == RUN_STATUS_FAILED
        assert repository.get_orchestration_for_run(session, run_id) is None
        assert repository.list_attempts_for_run(session, run_id) == []
    assert not list(workdir.glob("prompt-*.txt")), "the model must not have been called"


# --- Cancellation -------------------------------------------------------------------


def test_cancelling_a_queued_job_prevents_any_launch(
    client, session_factory, database_url, tmp_path, fake_docker
) -> None:
    run_id = make_run(client, max_parallel_attempts=1)
    enqueue(session_factory, run_id)
    with session_factory() as session:
        repository.request_job_cancellation(session, run_id=run_id)
    scenario, workdir = write_scenario(tmp_path, {"1": {}})

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: "Waiting for started runs" in (tmp_path / "dispatcher.log").read_text(),
            what="the dispatcher to reach its loop",
        )
        # Give it several poll intervals to prove it is not going to launch.
        time.sleep(1.0)
    finally:
        stop(process, handle)

    assert job_of(session_factory, run_id).status == JOB_STATUS_CANCELLED
    assert not (workdir / "inspect-started").exists(), "nothing may launch for a cancelled job"
    with session_factory() as session:
        assert repository.get_orchestration_for_run(session, run_id) is None


def test_cancelling_an_active_job_stops_its_work_and_sweeps_containers(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    run_id = make_run(client, max_parallel_attempts=1)
    inspect_now(run_id, session_factory, worker_settings)
    enqueue(session_factory, run_id)
    scenario, workdir = write_scenario(tmp_path, {"1": {"behaviour": "hang"}})

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(lambda: (workdir / "arrived-1").exists(), what="the attempt to be mid-proposal")
        attempt_pid = int((workdir / "pid-1").read_text())

        with session_factory() as session:
            repository.request_job_cancellation(session, run_id=run_id)

        wait_for(
            lambda: (job_of(session_factory, run_id).status == JOB_STATUS_CANCELLED),
            what="the job to become cancelled",
            timeout=120,
        )
    finally:
        stop(process, handle)

    assert not alive(attempt_pid), "the attempt worker must not outlive the cancellation"
    with session_factory() as session:
        orchestration = repository.get_orchestration_for_run(session, run_id)
        assert orchestration.status in ("interrupted", "failed")
        # The proposal's own record is preserved, not rewritten by the cancellation.
        attempt = repository.list_attempts_for_run(session, run_id)[0]
        assert attempt.status == "interrupted"

    swept = [
        line
        for line in fake_docker["log"].read_text().splitlines()
        if f"label=branchforge.orchestration={orchestration.id}" in line
    ]
    assert swept, "containers owned by the orchestration must be swept"


def test_cancelling_during_inspection_is_recorded_honestly(
    client, session_factory, database_url, tmp_path, fake_docker
) -> None:
    """The inspector has no SIGTERM handler, so it dies mid-flight.

    The run must not be left looking as though it is still being inspected by
    something that no longer exists.
    """
    run_id = make_run(client, max_parallel_attempts=1)
    enqueue(session_factory, run_id)
    scenario, workdir = write_scenario(tmp_path, {"1": {}}, inspect={"behaviour": "hang"})

    process, handle, _ = start_dispatcher(
        tmp_path, database_url, scenario, fake_docker, grace=2.0
    )
    try:
        # Wait for the *claim*, not merely for the process to start: only then is
        # the run actually `inspecting` and the reconciliation meaningful.
        wait_for(lambda: (workdir / "inspect-claimed").exists(), what="the inspector to claim the run")
        inspect_pid = int((workdir / "inspect-pid").read_text())
        with session_factory() as session:
            repository.request_job_cancellation(session, run_id=run_id)
        wait_for(
            lambda: (job_of(session_factory, run_id).status == JOB_STATUS_CANCELLED),
            what="the job to become cancelled",
            timeout=120,
        )
    finally:
        stop(process, handle)

    assert not alive(inspect_pid)
    with session_factory() as session:
        run = repository.get_run(session, run_id)
        inspection = repository.get_inspection_for_run(session, run_id)
        assert run.status == RUN_STATUS_FAILED, "a run is never left 'inspecting' by nothing"
        assert inspection is not None and inspection.error_kind == "cancelled"
        assert repository.get_orchestration_for_run(session, run_id) is None


# --- Shutdown ------------------------------------------------------------------------


def test_shutdown_leaves_untouched_queued_jobs_for_a_later_dispatcher(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    first_run = make_run(client, max_parallel_attempts=1)
    second_run = make_run(client, max_parallel_attempts=1)
    inspect_now(first_run, session_factory, worker_settings)
    inspect_now(second_run, session_factory, worker_settings)
    enqueue(session_factory, first_run)
    enqueue(session_factory, second_run)
    scenario, workdir = write_scenario(tmp_path, {"1": {"behaviour": "hang"}})

    process, handle, _ = start_dispatcher(
        tmp_path, database_url, scenario, fake_docker, grace=2.0
    )
    try:
        wait_for(lambda: (workdir / "arrived-1").exists(), what="the first job to be working")
    finally:
        code = stop(process, handle)

    assert code == worker.EXIT_INTERRUPTED
    assert job_of(session_factory, first_run).status == JOB_STATUS_INTERRUPTED
    second = job_of(session_factory, second_run)
    assert second.status == JOB_STATUS_QUEUED, "an untouched job stays available"
    assert second.started_at is None
    with session_factory() as session:
        assert repository.get_orchestration_for_run(session, second_run) is None


def test_a_failed_job_does_not_prevent_the_next_one(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    """The first job's inspection fails; the second, already inspected, still runs."""
    failing_run = make_run(client, max_parallel_attempts=1)
    good_run = make_run(client, max_parallel_attempts=1)
    inspect_now(good_run, session_factory, worker_settings)
    enqueue(session_factory, failing_run)
    enqueue(session_factory, good_run)
    scenario, workdir = write_scenario(
        tmp_path, {"1": {"patch": "correct"}}, inspect={"behaviour": "fail"}
    )

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    try:
        wait_for(
            lambda: (job_of(session_factory, good_run).status == JOB_STATUS_COMPLETED),
            what="the second job to complete",
            timeout=180,
        )
    finally:
        stop(process, handle)

    assert job_of(session_factory, failing_run).status == JOB_STATUS_FAILED
    assert job_of(session_factory, good_run).status == JOB_STATUS_COMPLETED
    with session_factory() as session:
        assert repository.get_orchestration_for_run(session, good_run) is not None


# --- Crash debris ----------------------------------------------------------------------


def test_a_job_left_running_by_a_dead_dispatcher_is_flagged_not_replayed(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    """Holding the lock proves no dispatcher is alive — not that its work stopped."""
    run_id = make_run(client, max_parallel_attempts=1)
    inspect_now(run_id, session_factory, worker_settings)
    job_id = enqueue(session_factory, run_id)
    with session_factory() as session:
        assert repository.claim_queued_job(session, job_id=job_id, dispatcher_id="dead-one")
    scenario, workdir = write_scenario(tmp_path, {"1": {"patch": "correct"}})

    process, handle, log_path = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    assert process.wait(timeout=60) == worker.EXIT_RECOVERY_REQUIRED
    handle.close()

    job = job_of(session_factory, run_id)
    assert job.status == JOB_STATUS_RUNNING, "it is not declared stopped — that is unknown"
    assert job.recovery_required is True
    assert job.notes and len(job.notes) == 1
    assert "not resumed" in job.notes[0].lower()
    assert "needs manual recovery" in log_path.read_text()
    assert not (workdir / "inspect-started").exists(), "nothing may be replayed"
    assert not list(workdir.glob("prompt-*.txt"))


def test_repeated_restarts_do_not_append_duplicate_recovery_notes(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    run_id = make_run(client, max_parallel_attempts=1)
    inspect_now(run_id, session_factory, worker_settings)
    job_id = enqueue(session_factory, run_id)
    with session_factory() as session:
        assert repository.claim_queued_job(session, job_id=job_id, dispatcher_id="dead-one")
    scenario, _ = write_scenario(tmp_path, {"1": {}})

    for attempt in range(3):
        process, handle, _ = start_dispatcher(
            tmp_path, database_url, scenario, fake_docker, name=f"restart{attempt}"
        )
        assert process.wait(timeout=60) == worker.EXIT_RECOVERY_REQUIRED
        handle.close()

    job = job_of(session_factory, run_id)
    assert len(job.notes) == 1, f"notes grew on every restart: {job.notes}"


def test_a_recovery_required_job_blocks_later_queued_work(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    """Draining more work beside possibly-orphaned containers is refused."""
    stuck_run = make_run(client, max_parallel_attempts=1)
    waiting_run = make_run(client, max_parallel_attempts=1)
    inspect_now(stuck_run, session_factory, worker_settings)
    inspect_now(waiting_run, session_factory, worker_settings)
    stuck_job = enqueue(session_factory, stuck_run)
    enqueue(session_factory, waiting_run)
    with session_factory() as session:
        assert repository.claim_queued_job(session, job_id=stuck_job, dispatcher_id="dead-one")
    scenario, workdir = write_scenario(tmp_path, {"1": {"patch": "correct"}})

    process, handle, _ = start_dispatcher(tmp_path, database_url, scenario, fake_docker)
    assert process.wait(timeout=60) == worker.EXIT_RECOVERY_REQUIRED
    handle.close()

    assert job_of(session_factory, waiting_run).status == JOB_STATUS_QUEUED
    with session_factory() as session:
        assert repository.get_orchestration_for_run(session, waiting_run) is None
    assert not list(workdir.glob("prompt-*.txt"))


# --- Descendants a killed coordinator leaves behind ---------------------------------------


def test_an_attempt_child_surviving_a_killed_orchestrator_is_stopped_and_reaped(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    """The gap this closes: an attempt child leads its own session.

    The orchestrator is made to ignore SIGTERM so the dispatcher must force-kill
    it, which leaves its attempt child orphaned — alive, in a session nothing the
    dispatcher signalled could reach. That child must be stopped before the
    container sweep, or it could still call the model and start containers after
    the job was reported cancelled.
    """
    run_id = make_run(client, max_parallel_attempts=1)
    inspect_now(run_id, session_factory, worker_settings)
    enqueue(session_factory, run_id)
    # The attempt child ignores SIGTERM and hangs; so the orchestrator cannot
    # shut down cleanly and the dispatcher's grace period must expire.
    scenario, workdir = write_scenario(tmp_path, {"1": {"behaviour": "ignore_term"}})

    process, handle, log_path = start_dispatcher(
        tmp_path,
        database_url,
        scenario,
        fake_docker,
        grace=3.0,
        orchestrator_grace=1.0,
        worker_grace=3.0,
    )
    try:
        wait_for(lambda: (workdir / "arrived-1").exists(), what="the attempt to be mid-proposal")
        attempt_pid = int((workdir / "pid-1").read_text())
        assert alive(attempt_pid)

        with session_factory() as session:
            repository.request_job_cancellation(session, run_id=run_id)

        wait_for(
            lambda: job_of(session_factory, run_id).status
            in (JOB_STATUS_CANCELLED, JOB_STATUS_FAILED),
            what="the job to finish winding down",
            timeout=180,
        )
    finally:
        stop(process, handle)

    # The decisive assertion: nothing of ours is still running.
    assert not alive(attempt_pid), (
        "an orphaned attempt worker survived cleanup; it could still call the model "
        "and start containers after the job was reported stopped"
    )

    with session_factory() as session:
        orchestration = repository.get_orchestration_for_run(session, run_id)
        attempt = repository.list_attempts_for_run(session, run_id)[0]
        assert attempt.status in ("interrupted", "failed")
        assert attempt.worker_pid is None, "a reaped worker's pid must be cleared"
        assert orchestration.status in ("interrupted", "failed")

    # The sweep ran for this orchestration's label, after the processes stopped.
    swept = [
        line
        for line in fake_docker["log"].read_text().splitlines()
        if f"label=branchforge.orchestration={orchestration.id}" in line
    ]
    assert swept, "the dispatcher must sweep the orchestration's containers itself"


def test_unconfirmed_cleanup_stops_the_dispatcher_from_taking_another_job(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
) -> None:
    """If containers cannot be confirmed gone, no further job is launched."""
    stuck_run = make_run(client, max_parallel_attempts=1)
    waiting_run = make_run(client, max_parallel_attempts=1)
    inspect_now(stuck_run, session_factory, worker_settings)
    inspect_now(waiting_run, session_factory, worker_settings)
    enqueue(session_factory, stuck_run)
    enqueue(session_factory, waiting_run)
    # `docker ps` keeps reporting a container, so removal can never be confirmed.
    fake_docker["ps_output"].write_text("deadbeefcafe\n")
    scenario, workdir = write_scenario(tmp_path, {"1": {"behaviour": "ignore_term"}})

    process, handle, _ = start_dispatcher(
        tmp_path,
        database_url,
        scenario,
        fake_docker,
        grace=3.0,
        orchestrator_grace=1.0,
        worker_grace=3.0,
    )
    try:
        wait_for(lambda: (workdir / "arrived-1").exists(), what="the attempt to be mid-proposal")
        with session_factory() as session:
            repository.request_job_cancellation(session, run_id=stuck_run)
        wait_for(
            lambda: job_of(session_factory, stuck_run).status == JOB_STATUS_FAILED,
            what="the job to fail on unconfirmed cleanup",
            timeout=180,
        )
    finally:
        stop(process, handle)

    stuck = job_of(session_factory, stuck_run)
    assert stuck.error_kind == repository.JOB_ERROR_CLEANUP_UNCONFIRMED
    assert job_of(session_factory, waiting_run).status == JOB_STATUS_QUEUED
    with session_factory() as session:
        assert repository.get_orchestration_for_run(session, waiting_run) is None
