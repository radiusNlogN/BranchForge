"""Real containers under a real orchestrator. Skipped without Docker — never faked.

Proposals are scripted (no model calls); verification is the real runner: real
snapshots of the fixture repository, real `git apply`, real `docker run`. This is
the evidence that ownership labels reach real containers, that cleanup removes
exactly this orchestration's containers, and that a signal leaves none behind.

Requires the runner image:
    docker build --provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest
"""

from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app import orchestrator, repository, runner, worker
from app.config import Settings
from app.models import ATTEMPT_STATUS_SUCCEEDED, ORCH_STATUS_INTERRUPTED, VERIFY_STATUS_INTERRUPTED
from app.runner import RunnerLimits
from tests.conftest import INSPECTABLE_PAYLOAD, FakeGitHub, ScriptedModelClient, agent_settings
from tests.orchestration_driver import child_command_for

pytestmark = pytest.mark.docker

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _image_id() -> str | None:
    if not runner.docker_available():
        return None
    try:
        return runner.resolve_image_id(Settings().verify_image, limits=RunnerLimits())
    except runner.DockerUnavailable:
        return None


IMAGE_ID = _image_id()
requires_docker = pytest.mark.skipif(
    IMAGE_ID is None,
    reason="Docker daemon or runner image unavailable. Build it with: docker build "
    "--provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest",
)


def labelled(key: str, value: str) -> list[str]:
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"label={key}={value}"],
        capture_output=True, text=True, timeout=30,
    )
    return [line for line in listed.stdout.split() if line]


def running(container_id: str) -> bool:
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True, text=True, timeout=30,
    )
    return inspected.stdout.strip() == "true"


def ready_run(client, session_factory, worker_settings, attempts: int) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, max_parallel_attempts=attempts)
    run_id = client.post("/api/runs", json=payload).json()["id"]
    worker.inspect_run(
        run_id, session_factory=session_factory,
        client_factory=FakeGitHub().client_factory, config=worker_settings, out=io.StringIO(),
    )
    return run_id


def scenario_file(tmp_path, attempts: dict) -> Path:
    workdir = tmp_path / "work"
    workdir.mkdir()
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"workdir": str(workdir), "image_id": IMAGE_ID, "attempts": attempts}))
    return path


@requires_docker
def test_a_real_orchestration_verifies_each_attempt_and_leaves_no_containers(
    client, session_factory, worker_settings, database_url, tmp_path
):
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario = scenario_file(tmp_path, {
        "1": {"patch": "correct", "runner": "docker"},
        "2": {"patch": "incorrect", "runner": "docker"},
        "3": {"behaviour": "fail"},
    })
    out = io.StringIO()
    code = orchestrator.orchestrate_run(
        run_id,
        session_factory=session_factory,
        model_factory=lambda c: ScriptedModelClient([]),
        config=agent_settings(database_url=database_url, orchestrator_max_concurrency=3,
                              verify_test_timeout_seconds=120),
        image_id=IMAGE_ID,
        child_command=child_command_for(str(scenario)),
        out=out,
    )
    assert code == worker.EXIT_OK, out.getvalue()

    with session_factory() as db:
        orchestration = repository.get_orchestration_for_run(db, run_id)
        rows = {a.attempt_index: (a, v) for a, v in repository.list_attempts_with_verifications(db, run_id)}
    assert rows[1][1].outcome == "fix_demonstrated"
    assert rows[1][1].image_id == IMAGE_ID
    assert rows[2][1].outcome == "regressions"
    assert rows[3][0].status == "failed" and rows[3][1] is None
    assert orchestration.recommended_attempt_index == 1
    # The labels really reached the containers: the recorded argv carries them.
    assert f"branchforge.attempt={rows[1][0].id}" in rows[1][1].runner_args
    assert f"branchforge.orchestration={orchestration.id}" in rows[1][1].runner_args
    assert labelled(runner.LABEL_ORCHESTRATION, orchestration.id) == []


@requires_docker
def test_a_signal_removes_this_orchestrations_containers_and_nobody_elses(
    client, session_factory, worker_settings, database_url, tmp_path
):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario = scenario_file(tmp_path, {
        "1": {"patch": "hanging", "runner": "docker"},
        "2": {"patch": "hanging", "runner": "docker"},
    })

    decoys: list[str] = []
    for labels in (["--label", f"{runner.LABEL_ORCHESTRATION}={uuid.uuid4()}"], []):
        started = subprocess.run(
            ["docker", "run", "-d", "--rm", "--network=none", *labels, IMAGE_ID,
             "python", "-c", "import time; time.sleep(600)"],
            capture_output=True, text=True, timeout=60,
        )
        assert started.returncode == 0, started.stderr
        decoys.append(started.stdout.strip())

    log = open(tmp_path / "driver.log", "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.orchestration_driver", "--run-id", run_id,
         "--scenario", str(scenario), "--database-url", database_url, "--concurrency", "2",
         "--grace", "5", "--docker-binary", "docker", "--test-timeout", "300"],
        cwd=BACKEND_ROOT, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        orchestration_id = None
        deadline = time.monotonic() + 240
        # Wait for a condition, not a duration: both patched runs hang inside
        # containers until something removes them.
        while True:
            assert time.monotonic() < deadline, (tmp_path / "driver.log").read_text()
            if orchestration_id is None:
                with session_factory() as db:
                    found = repository.get_orchestration_for_run(db, run_id)
                    orchestration_id = found.id if found else None
            if orchestration_id and len(labelled(runner.LABEL_ORCHESTRATION, orchestration_id)) == 2:
                break
            time.sleep(0.5)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=180) == worker.EXIT_INTERRUPTED, (tmp_path / "driver.log").read_text()

        assert labelled(runner.LABEL_ORCHESTRATION, orchestration_id) == []
        assert all(running(decoy) for decoy in decoys), "another owner's containers were touched"

        with session_factory() as db:
            orchestration = repository.get_orchestration_for_run(db, run_id)
            rows = repository.list_attempts_with_verifications(db, run_id)
        assert orchestration.status == ORCH_STATUS_INTERRUPTED
        for attempt, record in rows:
            assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
            assert record is not None and record.status == VERIFY_STATUS_INTERRUPTED
    finally:
        if process.poll() is None:
            process.kill()
        log.close()
        for decoy in decoys:
            subprocess.run(["docker", "rm", "-f", decoy], capture_output=True, timeout=60)
