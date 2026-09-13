"""The coordinator, driven through REAL child processes.

Children are `tests.orchestration_child`: the real `run-attempt` code path with
the model, GitHub, snapshot, and container runner scripted. No network, no
Docker (a fake `docker` binary records what the coordinator asks of it).

Overlap is proven with a file barrier sized to the effective concurrency — it can
only release if that many children are simultaneously mid-proposal — never with
elapsed-time thresholds.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import agent, orchestrator, repository, worker
from app.models import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_INTERRUPTED,
    ATTEMPT_STATUS_SUCCEEDED,
    ORCH_STATUS_COMPLETED,
    ORCH_STATUS_FAILED,
    ORCH_STATUS_INTERRUPTED,
    VERIFY_STATUS_COMPLETED,
    VERIFY_STATUS_FAILED,
    VERIFY_STATUS_INTERRUPTED,
)
from app.orchestrator import OutputRelay
from tests.conftest import INSPECTABLE_PAYLOAD, FakeGitHub, ScriptedModelClient, agent_settings
from tests.orchestration_driver import child_command_for

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


def ready_run(client, session_factory, worker_settings, attempts: int) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, max_parallel_attempts=attempts)
    run_id = client.post("/api/runs", json=payload).json()["id"]
    assert worker.inspect_run(
        run_id, session_factory=session_factory,
        client_factory=FakeGitHub().client_factory, config=worker_settings, out=io.StringIO(),
    ) == worker.EXIT_OK
    return run_id


def write_scenario(tmp_path, attempts: dict, **extra) -> tuple[Path, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"workdir": str(workdir), "image_id": IMAGE_ID,
                                    "attempts": attempts, **extra}))
    return scenario, workdir


def orchestrate(run_id, session_factory, database_url, scenario, fake_docker, *, concurrency=3, **config):
    out = io.StringIO()
    code = orchestrator.orchestrate_run(
        run_id,
        session_factory=session_factory,
        model_factory=lambda c: ScriptedModelClient([]),
        config=agent_settings(
            database_url=database_url,
            orchestrator_max_concurrency=concurrency,
            verify_docker_binary=fake_docker["binary"],
            **config,
        ),
        image_id=IMAGE_ID,
        child_command=child_command_for(str(scenario)),
        out=out,
    )
    return code, out.getvalue()


def state(session_factory, run_id):
    with session_factory() as db:
        orchestration = repository.get_orchestration_for_run(db, run_id)
        rows = repository.list_attempts_with_verifications(db, run_id)
        events = {
            a.attempt_index: [e.summary for e in repository.list_attempt_events(db, a.id, limit=500)]
            for a, _ in rows
        }
        return orchestration, rows, events


def by_index(rows):
    return {a.attempt_index: (a, v) for a, v in rows}


def read_max_active(workdir: Path) -> int:
    return int((workdir / "max_active").read_text())


# --- Concurrency -----------------------------------------------------------------


def test_attempts_overlap_and_all_three_complete(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario, workdir = write_scenario(
        tmp_path,
        {"1": {"patch": "correct"}, "2": {"patch": "ineffective", "runner": "failing"},
         "3": {"patch": "incorrect", "runner": "regressing"}},
        barrier=3,
    )
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=3)
    assert code == worker.EXIT_OK, output

    orchestration, rows, _ = state(session_factory, run_id)
    assert orchestration.status == ORCH_STATUS_COMPLETED
    assert read_max_active(workdir) == 3, "the barrier released, so all three overlapped"
    outcomes = {i: v.outcome for i, (_, v) in by_index(rows).items()}
    assert outcomes == {1: "fix_demonstrated", 2: "still_failing", 3: "regressions"}
    assert orchestration.recommended_attempt_index == 1
    assert orchestration.comparison["headline"] == "Recommended: attempt 1"


def test_active_pipelines_never_exceed_the_limit(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    """Limit 2, three attempts: two overlap (barrier of 2), never three."""
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario, workdir = write_scenario(
        tmp_path, {"1": {}, "2": {}, "3": {}}, barrier=2,
    )
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output
    assert read_max_active(workdir) == 2
    orchestration, rows, _ = state(session_factory, run_id)
    assert orchestration.effective_concurrency == 2
    assert all(a.status == ATTEMPT_STATUS_SUCCEEDED for a, _ in rows)
    assert any("At most 2 attempt pipeline(s) ran at once" in n for n in orchestration.notes)


def test_a_limit_of_one_runs_sequentially(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, workdir = write_scenario(tmp_path, {"1": {}, "2": {}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=1)
    assert code == worker.EXIT_OK, output
    assert read_max_active(workdir) == 1


# --- Failure isolation --------------------------------------------------------------


def test_a_failed_attempt_does_not_stop_a_successful_sibling(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, _ = write_scenario(tmp_path, {"1": {"behaviour": "fail"}, "2": {"patch": "correct"}}, barrier=2)
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output

    orchestration, rows, _ = state(session_factory, run_id)
    first, second = by_index(rows)[1], by_index(rows)[2]
    assert first[0].status == ATTEMPT_STATUS_FAILED and first[1] is None
    assert first[0].pipeline_error_kind is None, "a failed proposal is a finished pipeline"
    assert second[0].status == ATTEMPT_STATUS_SUCCEEDED
    assert second[1].outcome == "fix_demonstrated"
    assert orchestration.recommended_attempt_index == 2


def test_a_child_crashing_mid_proposal_is_recorded_and_siblings_finish(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, _ = write_scenario(tmp_path, {"1": {"behaviour": "crash_in_proposal"}, "2": {"patch": "correct"}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output

    _, rows, _ = state(session_factory, run_id)
    crashed = by_index(rows)[1][0]
    assert crashed.status == ATTEMPT_STATUS_FAILED
    assert crashed.error_kind == "worker_exited"
    assert "exit code 9" in crashed.error_message
    assert by_index(rows)[2][1].outcome == "fix_demonstrated"


@pytest.mark.parametrize("exit_code", [0, 3])
def test_exit_between_saved_proposal_and_verification_is_an_incomplete_pipeline(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker, exit_code
):
    """The deterministic boundary crash: patch saved, verification never claimed.

    Exit 0 included — completion is decided from the database, not the exit code.
    """
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, _ = write_scenario(
        tmp_path, {"1": {"patch": "correct", "after_proposal_exit": exit_code}, "2": {"patch": "correct"}},
    )
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output

    orchestration, rows, _ = state(session_factory, run_id)
    attempt, record = by_index(rows)[1]
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED, "the proposal's success is preserved"
    assert record is None
    assert attempt.pipeline_error_kind == "verification_not_started"
    assert "verification never started" in attempt.pipeline_error_message
    assert f"exit code {exit_code}" in attempt.pipeline_error_message

    candidate = next(c for c in orchestration.comparison["candidates"] if c["attempt_index"] == 1)
    assert candidate["eligible"] is False
    assert any("verification_not_started" in r for r in candidate["reasons"])
    assert orchestration.recommended_attempt_index == 2

    body = client.get(f"/api/runs/{run_id}").json()
    api_attempt = next(a for a in body["attempts"] if a["attempt_index"] == 1)
    assert api_attempt["status"] == "succeeded"
    assert api_attempt["pipeline_error_kind"] == "verification_not_started"
    assert api_attempt["verification"] is None


def test_a_child_dying_mid_verification_fails_that_verification_only(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 1)
    scenario, _ = write_scenario(tmp_path, {"1": {"patch": "correct", "runner": "crash"}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=1)
    assert code == worker.EXIT_OK, output

    orchestration, rows, _ = state(session_factory, run_id)
    attempt, record = by_index(rows)[1]
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert record.status == VERIFY_STATUS_FAILED
    assert record.error_kind == "worker_exited"
    assert attempt.pipeline_error_kind == "verification_unfinished"
    assert orchestration.recommended_attempt_index is None
    assert orchestration.comparison["headline"] == "No demonstrated fix"


# --- Isolation -----------------------------------------------------------------------


def test_attempts_are_isolated_but_share_one_commit_and_report(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario, workdir = write_scenario(
        tmp_path, {"1": {"patch": "correct"}, "2": {"patch": "ineffective", "runner": "failing"},
                   "3": {"patch": "incorrect", "runner": "regressing"}},
    )
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker)
    assert code == worker.EXIT_OK, output
    orchestration, rows, events = state(session_factory, run_id)

    # Events: each attempt read only its own file.
    for index in (1, 2, 3):
        reads = [s for s in events[index] if s.startswith("Read ")]
        assert reads == [f"Read src/file_{index}.py"], events[index]

    # Patches and fingerprints are per attempt.
    attempts = by_index(rows)
    assert len({a.diff for a, _ in attempts.values()}) == 3
    for attempt, record in attempts.values():
        assert record.attempt_id == attempt.id
        assert record.patch_sha256 == __import__("hashlib").sha256(attempt.diff.encode()).hexdigest()

    # Workspaces: distinct per attempt, each under its own attempt-<n> directory.
    seen = {}
    for index in (1, 2, 3):
        paths = (workdir / f"workspaces-{index}.txt").read_text().split()
        assert paths and all(f"{os.sep}attempt-{index}{os.sep}" in p for p in paths), paths
        seen[index] = set(paths)
    assert not (seen[1] & seen[2]) and not (seen[2] & seen[3]) and not (seen[1] & seen[3])
    # ...and the coordinator removed the whole workspace root afterwards.
    assert not os.path.exists(orchestration.workspace_root)

    # One commit, one report; the first prompts differ only in the emphasis section.
    assert {a.commit_sha for a, _ in attempts.values()} == {orchestration.commit_sha}
    prompts = {i: (workdir / f"prompt-{i}.txt").read_text() for i in (1, 2, 3)}
    shared = {p.split(agent.EMPHASIS_HEADING)[0] for p in prompts.values()}
    assert len(shared) == 1
    for index, prompt in prompts.items():
        assert prompt.endswith(attempts[index][0].emphasis_text)
    assert len({a.emphasis_key for a, _ in attempts.values()}) == 3


# --- Cleanup confirmation ----------------------------------------------------------


def test_unconfirmed_cleanup_stops_launching_and_is_recorded(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    """If a leftover container cannot be confirmed gone, no queued work starts."""
    fake_docker["ps_output"].write_text("deadbeefcafe\n")  # "still present" forever
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario, workdir = write_scenario(tmp_path, {"1": {}, "2": {}, "3": {}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=1)
    assert code == worker.EXIT_ORCHESTRATION_FAILED, output

    orchestration, rows, _ = state(session_factory, run_id)
    assert orchestration.status == ORCH_STATUS_FAILED
    assert orchestration.error_kind == "cleanup_unconfirmed"
    attempts = by_index(rows)
    assert attempts[1][0].status == ATTEMPT_STATUS_SUCCEEDED
    for index in (2, 3):
        assert attempts[index][0].status == ATTEMPT_STATUS_INTERRUPTED
        assert attempts[index][0].pipeline_error_kind == "not_launched"
        assert "cleanup" in attempts[index][0].pipeline_error_message
    assert not (workdir / "pid-2").exists() and not (workdir / "pid-3").exists()


def test_cleanup_targets_only_this_orchestrations_labels(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, _ = write_scenario(tmp_path, {"1": {}, "2": {}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output
    orchestration, rows, _ = state(session_factory, run_id)

    owned = {f"label=branchforge.orchestration={orchestration.id}"} | {
        f"label=branchforge.attempt={a.id}" for a, _ in rows
    }
    filters = set()
    for line in fake_docker["log"].read_text().splitlines():
        parts = line.split()
        assert parts[0] == "ps", f"unexpected docker call {line!r} (nothing was left to remove)"
        filters.add(parts[parts.index("--filter") + 1])
    assert filters == owned


# --- Signals: a real coordinator process --------------------------------------------


def start_driver(tmp_path, run_id, database_url, scenario, fake_docker, *, concurrency=2, grace=5.0):
    log = open(tmp_path / "driver.log", "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.orchestration_driver",
         "--run-id", run_id, "--scenario", str(scenario), "--database-url", database_url,
         "--concurrency", str(concurrency), "--grace", str(grace),
         "--docker-binary", fake_docker["binary"]],
        cwd=BACKEND_ROOT, stdout=log, stderr=subprocess.STDOUT,
    )
    return process, log


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


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_launching_terminates_children_and_records_interruption(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker, signum
):
    run_id = ready_run(client, session_factory, worker_settings, 3)
    scenario, workdir = write_scenario(
        tmp_path, {"1": {"behaviour": "hang"}, "2": {"behaviour": "hang"}, "3": {}},
    )
    process, log = start_driver(tmp_path, run_id, database_url, scenario, fake_docker, concurrency=2)
    try:
        wait_for(lambda: (workdir / "arrived-1").exists() and (workdir / "arrived-2").exists(),
                 what="both children mid-proposal")
        pids = [int((workdir / f"pid-{i}").read_text()) for i in (1, 2)]
        process.send_signal(signum)
        assert process.wait(timeout=60) == worker.EXIT_INTERRUPTED
    finally:
        if process.poll() is None:
            process.kill()
        log.close()

    output = (tmp_path / "driver.log").read_text()
    assert f"Received {signal.Signals(signum).name}" in output
    assert not any(alive(pid) for pid in pids), "children must not outlive the coordinator"
    assert not (workdir / "pid-3").exists(), "no new attempt may launch after the signal"

    orchestration, rows, _ = state(session_factory, run_id)
    assert orchestration.status == ORCH_STATUS_INTERRUPTED
    attempts = by_index(rows)
    for index in (1, 2):
        assert attempts[index][0].status == ATTEMPT_STATUS_INTERRUPTED
        assert attempts[index][0].pipeline_error_kind == "interrupted"
    assert attempts[3][0].status == ATTEMPT_STATUS_INTERRUPTED
    assert attempts[3][0].pipeline_error_kind == "not_launched"
    assert orchestration.comparison["complete"] is False
    assert orchestration.comparison["headline"] == "No demonstrated fix"

    swept = [line for line in fake_docker["log"].read_text().splitlines()
             if f"label=branchforge.orchestration={orchestration.id}" in line]
    assert swept, "the coordinator must sweep its own containers on shutdown"
    assert not os.path.exists(orchestration.workspace_root)


def test_a_child_ignoring_sigterm_is_killed_after_the_grace_period(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
):
    run_id = ready_run(client, session_factory, worker_settings, 1)
    scenario, workdir = write_scenario(tmp_path, {"1": {"behaviour": "ignore_term"}})
    process, log = start_driver(tmp_path, run_id, database_url, scenario, fake_docker, concurrency=1, grace=1.0)
    try:
        wait_for(lambda: (workdir / "arrived-1").exists(), what="the child mid-proposal")
        pid = int((workdir / "pid-1").read_text())
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == worker.EXIT_INTERRUPTED
    finally:
        if process.poll() is None:
            process.kill()
        log.close()
    assert "ignored SIGTERM; killing it" in (tmp_path / "driver.log").read_text()
    assert not alive(pid)
    _, rows, _ = state(session_factory, run_id)
    assert rows[0][0].status == ATTEMPT_STATUS_INTERRUPTED


def test_an_interruption_during_verification_leaves_the_proposal_succeeded(
    client, session_factory, worker_settings, database_url, tmp_path, fake_docker
):
    run_id = ready_run(client, session_factory, worker_settings, 1)
    scenario, workdir = write_scenario(tmp_path, {"1": {"patch": "correct", "runner": "hang"}})
    process, log = start_driver(tmp_path, run_id, database_url, scenario, fake_docker, concurrency=1)
    try:
        wait_for(lambda: (workdir / "workspaces-1.txt").exists(), what="the baseline run to start")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == worker.EXIT_INTERRUPTED
    finally:
        if process.poll() is None:
            process.kill()
        log.close()
    _, rows, _ = state(session_factory, run_id)
    attempt, record = rows[0]
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED, "verification must never rewrite the proposal"
    assert record.status == VERIFY_STATUS_INTERRUPTED
    assert attempt.pipeline_error_kind == "verification_unfinished"


# --- The API view ---------------------------------------------------------------------


def test_the_api_returns_every_attempt_and_the_orchestration(client, session_factory, worker_settings, database_url, tmp_path, fake_docker):
    run_id = ready_run(client, session_factory, worker_settings, 2)
    scenario, _ = write_scenario(tmp_path, {"1": {"patch": "correct"}, "2": {"behaviour": "fail"}})
    code, output = orchestrate(run_id, session_factory, database_url, scenario, fake_docker, concurrency=2)
    assert code == worker.EXIT_OK, output

    body = client.get(f"/api/runs/{run_id}").json()
    assert [a["attempt_index"] for a in body["attempts"]] == [1, 2]
    first, second = body["attempts"]
    assert first["verification"]["status"] == VERIFY_STATUS_COMPLETED
    assert first["verification"]["outcome"] == "fix_demonstrated"
    assert first["events_total"] == len(first["events"]) > 0
    assert first["emphasis_text"] and second["emphasis_text"] != first["emphasis_text"]
    assert second["status"] == "failed" and second["verification"] is None
    assert first["input_tokens"] == 200

    orchestration = body["orchestration"]
    assert orchestration["status"] == "completed"
    assert orchestration["recommended_attempt_index"] == 1
    assert orchestration["effective_concurrency"] == 2
    assert "execution_config" not in orchestration
    assert orchestration["comparison"]["tie_breaker"].endswith("not evidence of better code.")

    listed = client.get("/api/runs").json()
    assert "orchestration" not in listed[0] and "attempts" not in listed[0]


# --- Output relay ----------------------------------------------------------------------


class _Sink:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


def test_one_enormous_line_is_bounded_and_draining_continues():
    sink = _Sink()
    relay = OutputRelay("[attempt 1]", sink, max_line_chars=100)
    relay.feed(b"x" * 5_000_000)
    assert len(relay.buffer) == 0 and relay.dropping
    relay.feed(b"y" * 1_000_000 + b"\nnext line\n")
    relay.close()
    text = "".join(sink.lines)
    assert "[line truncated]" in text
    assert "[attempt 1] next line" in text
    assert "y" not in text, "the rest of the oversized line is discarded, not retained"
    assert relay.bytes_read == 6_000_011


def test_forwarding_failure_does_not_stop_draining():
    class Broken:
        def write(self, text):
            raise BrokenPipeError("terminal went away")

        def flush(self):
            pass

    async def scenario():
        stream = asyncio.StreamReader()
        stream.feed_data(b"line one\n" * 1000 + b"z" * 300_000)
        stream.feed_eof()
        relay = OutputRelay("[attempt 1]", Broken(), max_line_chars=50)
        await relay.drain(stream)
        return relay

    relay = asyncio.run(scenario())
    assert relay.forwarding is False
    assert relay.bytes_read == 9_000 + 300_000, "every byte was still read to EOF"
