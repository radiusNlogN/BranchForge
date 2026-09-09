"""The `verify` worker command: preconditions, claiming, and persistence.

No Docker and no network: the container runner and the snapshot fetcher are both
injected. What is exercised is the ordering that matters — nothing expensive
starts before the claim, and losing the claim starts nothing at all.
"""

from __future__ import annotations

import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repository, runner, verification, worker
from app.config import Settings
from app.models import RUN_STATUS_READY, VERIFY_STATUS_COMPLETED, VERIFY_STATUS_FAILED
from app.runner import RunResult
from app.snapshot import SnapshotResult, SnapshotUnavailable
from tests import fixture_support as fx
from tests.conftest import (
    INSPECTABLE_PAYLOAD,
    FakeGitHub,
    ScriptedModelClient,
    agent_settings,
    make_turn,
    submit_call,
)

ADD = "tests/test_calc.py::test_add"
IDENTITY = "tests/test_calc.py::test_identity"
ALPHA = "tests/test_calc.py::test_cases[alpha]"
BETA = "tests/test_calc.py::test_cases[beta]"
ALL_TESTS = [ADD, IDENTITY, ALPHA, BETA]
IMAGE_ID = "sha256:testimage"


def make_report(outcomes, *, collected=None, exitstatus=None):
    collected = list(outcomes) if collected is None else collected
    if exitstatus is None:
        exitstatus = 1 if any(o in ("failed", "error") for o in outcomes.values()) else 0
    return {
        "schema": "branchforge.report.v1",
        "exitstatus": exitstatus,
        "collected": collected,
        "collect_errors": [],
        "tests": {n: {"outcome": o, "phases": {}} for n, o in outcomes.items()},
        "counts": {},
        "truncated": False,
    }


def make_run_result(report, *, exit_code=1, log="container log"):
    return RunResult(
        exit_code=exit_code, log=log, log_truncated=False, duration_seconds=0.2,
        report=report, argv=["docker", "run", "--network=none", IMAGE_ID],
    )


BASELINE = make_report({ADD: "failed", IDENTITY: "passed", ALPHA: "passed", BETA: "passed"})
ALL_PASSING = make_report({n: "passed" for n in ALL_TESTS}, exitstatus=0)


def local_snapshot(*, owner, repo, commit_sha, destination):
    fx.copy_sample_repo(destination)
    return SnapshotResult(
        commit_sha=commit_sha, root=destination, file_count=4,
        decompressed_bytes=1000, download_bytes=500,
    )


class PhaseRunner:
    """Returns a canned result per workspace phase, and counts its calls."""

    def __init__(self, by_phase):
        self.by_phase = by_phase
        self.calls = 0

    def __call__(self, *, workspace, results_dir, image_id):
        self.calls += 1
        phase = os.path.basename(workspace)
        return self.by_phase[phase]


def fixing_runner():
    return PhaseRunner(
        {"baseline": make_run_result(BASELINE), "comparison": make_run_result(ALL_PASSING, exit_code=0)}
    )


@pytest.fixture
def verify_settings() -> Settings:
    return Settings(database_url="sqlite://", verify_image="branchforge-runner-python:test")


def proposed_run(client: TestClient, session_factory, worker_settings) -> str:
    """A run taken all the way to a stored, succeeded patch attempt."""
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=FakeGitHub().client_factory,
        config=worker_settings,
    )
    model = ScriptedModelClient(
        [make_turn(tool_calls=[submit_call(diff=fx.correct_patch())])]
    )
    assert worker.propose_patch(
        run_id,
        session_factory=session_factory,
        client_factory=FakeGitHub().client_factory,
        model_factory=lambda config: model,
        config=agent_settings(),
        out=io.StringIO(),
    ) == worker.EXIT_OK
    return run_id


def run_verify(run_id, session_factory, config, *, test_runner=None, snapshot_fetcher=None):
    out = io.StringIO()
    code = worker.verify_patch(
        run_id,
        session_factory=session_factory,
        config=config,
        image_id=IMAGE_ID,
        snapshot_fetcher=snapshot_fetcher or local_snapshot,
        test_runner=test_runner or fixing_runner(),
        out=out,
    )
    return code, out.getvalue()


# --- Preconditions ----------------------------------------------------------


def test_an_unknown_run_is_reported(session_factory, verify_settings):
    code, output = run_verify("11111111-2222-3333-4444-555555555555", session_factory, verify_settings)
    assert code == worker.EXIT_RUN_NOT_FOUND
    assert "No run found" in output


def test_a_run_without_a_patch_cannot_be_verified(client, session_factory, worker_settings, verify_settings):
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    worker.inspect_run(
        run_id, session_factory=session_factory,
        client_factory=FakeGitHub().client_factory, config=worker_settings,
    )
    code, output = run_verify(run_id, session_factory, verify_settings)
    assert code == worker.EXIT_NO_PATCH
    assert "no patch attempt" in output
    assert "worker propose" in output


def test_docker_unavailability_creates_no_verification_row(
    client, session_factory, worker_settings, verify_settings, monkeypatch
):
    """A stopped daemon must not leave a claimed verification behind."""
    run_id = proposed_run(client, session_factory, worker_settings)

    def boom(image_ref, *, limits):
        raise runner.DockerUnavailable("The Docker daemon is not reachable.")

    monkeypatch.setattr(runner, "resolve_image_id", boom)
    out = io.StringIO()
    code = worker.verify_patch(
        run_id, session_factory=session_factory, config=verify_settings, out=out
    )
    assert code == worker.EXIT_DOCKER_UNAVAILABLE
    assert "No verification was created." in out.getvalue()

    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, run_id)
        assert repository.get_verification_for_attempt(db, attempt.id) is None


# --- The happy path ---------------------------------------------------------


def test_a_successful_verification_persists_everything(
    client, session_factory, worker_settings, verify_settings
):
    run_id = proposed_run(client, session_factory, worker_settings)
    code, output = run_verify(run_id, session_factory, verify_settings)
    assert code == worker.EXIT_OK

    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, run_id)
        record = repository.get_verification_for_attempt(db, attempt.id)

        assert record.status == VERIFY_STATUS_COMPLETED
        assert record.outcome == verification.OUTCOME_FIX_DEMONSTRATED
        assert record.patch_applied is True
        assert record.commit_sha == attempt.commit_sha
        assert record.patch_sha256 == verification.patch_fingerprint(attempt.diff)
        assert record.profile == "python-pytest"
        assert record.image_id == IMAGE_ID
        assert record.runner_args and "--network=none" in record.runner_args
        assert record.baseline_summary["kind"] == verification.RUN_TESTS_FAILED
        assert record.patched_summary["kind"] == verification.RUN_OK
        assert record.comparison["fixed"] == [ADD]
        assert record.baseline_log and record.patched_log
        assert record.completed_at is not None
        # Supplemental was not needed: the patch touched no test files.
        assert record.supplemental_summary is None

    assert "Outcome: fix_demonstrated" in output
    assert "not proof the patch is correct" in output


def test_verification_never_changes_the_run_or_attempt_status(
    client, session_factory, worker_settings, verify_settings
):
    """`ready` describes inspection and `succeeded` describes the proposal."""
    run_id = proposed_run(client, session_factory, worker_settings)
    failing = PhaseRunner(
        {"baseline": make_run_result(BASELINE), "comparison": make_run_result(BASELINE)}
    )
    assert run_verify(run_id, session_factory, verify_settings, test_runner=failing)[0] == worker.EXIT_OK

    with session_factory() as db:
        run = repository.get_run(db, run_id)
        attempt = repository.get_patch_attempt_for_run(db, run_id)
        record = repository.get_verification_for_attempt(db, attempt.id)
        assert run.status == RUN_STATUS_READY
        assert attempt.status == "succeeded"
        assert record.outcome == verification.OUTCOME_STILL_FAILING


def test_a_snapshot_failure_is_recorded_on_the_verification(
    client, session_factory, worker_settings, verify_settings
):
    run_id = proposed_run(client, session_factory, worker_settings)

    def unavailable(*, owner, repo, commit_sha, destination):
        raise SnapshotUnavailable("No source archive for commit abcdef.")

    code, output = run_verify(
        run_id, session_factory, verify_settings, snapshot_fetcher=unavailable
    )
    assert code == worker.EXIT_VERIFICATION_FAILED

    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, run_id)
        record = repository.get_verification_for_attempt(db, attempt.id)
        assert record.status == VERIFY_STATUS_FAILED
        assert record.error_kind == "snapshot_unavailable"
        assert "No source archive" in record.error_message


def test_the_api_returns_the_persisted_verification(
    client, session_factory, worker_settings, verify_settings
):
    run_id = proposed_run(client, session_factory, worker_settings)
    run_verify(run_id, session_factory, verify_settings)

    payload = client.get(f"/api/runs/{run_id}").json()
    record = payload["verification"]
    assert record["status"] == "completed"
    assert record["outcome"] == "fix_demonstrated"
    assert record["comparison"]["fixed"] == [ADD]
    assert record["baseline_summary"]["kind"] == "tests_failed"
    assert record["patch_applied"] is True

    # The list endpoint stays lean.
    listed = client.get("/api/runs").json()
    assert "verification" not in listed[0]


# --- Claiming ---------------------------------------------------------------


def test_a_second_verification_is_refused(
    client, session_factory, worker_settings, verify_settings
):
    run_id = proposed_run(client, session_factory, worker_settings)
    assert run_verify(run_id, session_factory, verify_settings)[0] == worker.EXIT_OK

    second = fixing_runner()
    code, output = run_verify(run_id, session_factory, verify_settings, test_runner=second)
    assert code == worker.EXIT_NOT_CLAIMED
    assert "already has a verification" in output
    assert second.calls == 0, "the loser must start no container"


def test_concurrent_verifications_permit_exactly_one_runner(
    client, session_factory, worker_settings, verify_settings, database_url
):
    """Two workers, two independent engines, one temporary file database."""
    run_id = proposed_run(client, session_factory, worker_settings)

    barrier = threading.Barrier(2)
    runners = [fixing_runner(), fixing_runner()]

    def attempt(index: int) -> int:
        engine = create_engine(
            database_url, connect_args={"check_same_thread": False, "timeout": 30}
        )
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        barrier.wait(timeout=10)
        return worker.verify_patch(
            run_id,
            session_factory=factory,
            config=verify_settings,
            image_id=IMAGE_ID,
            snapshot_fetcher=local_snapshot,
            test_runner=runners[index],
            out=io.StringIO(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(attempt, [0, 1]))

    assert sorted(codes) == [worker.EXIT_OK, worker.EXIT_NOT_CLAIMED]
    # Exactly one worker ran containers; the loser ran none.
    assert sorted(r.calls for r in runners) == [0, 2]

    with session_factory() as db:
        attempt_row = repository.get_patch_attempt_for_run(db, run_id)
        assert repository.get_verification_for_attempt(db, attempt_row.id) is not None
