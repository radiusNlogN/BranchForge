"""Orchestration ownership: claims, preflight, frozen settings, and ambiguity.

Contention uses the established pattern — a barrier plus independent engines
against one temporary file database — because a naive check-then-insert produces
two winners under it.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import agent, orchestrator, repository, runner, worker
from app.model_client import ModelNotConfigured
from app.models import ATTEMPT_STATUS_QUEUED, ATTEMPT_STATUS_SUCCEEDED
from tests.conftest import (
    COMMIT_SHA,
    INSPECTABLE_PAYLOAD,
    FakeGitHub,
    ScriptedModelClient,
    agent_settings,
    make_turn,
    read_call,
    submit_call,
)

IMAGE_ID = "sha256:testimage"


def inspected_run(client, session_factory, worker_settings, attempts: int = 2) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, max_parallel_attempts=attempts)
    run_id = client.post("/api/runs", json=payload).json()["id"]
    assert worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=FakeGitHub().client_factory,
        config=worker_settings,
        out=io.StringIO(),
    ) == worker.EXIT_OK
    return run_id


def claim(db, run_id, *, requested=2, config=None):
    config = config or agent_settings()
    return repository.claim_orchestration(
        db,
        run_id=run_id,
        requested_attempts=requested,
        concurrency_limit=2,
        effective_concurrency=min(2, requested),
        model="scripted-model-1",
        commit_sha=COMMIT_SHA,
        profile="python-pytest",
        image_ref="branchforge-runner-python:1",
        image_id=IMAGE_ID,
        workspace_root="/nonexistent",
        execution_config=config.frozen_execution_config(),
        emphases=[agent.emphasis_for(i) for i in range(1, requested + 1)],
    )


def never_called(spec):
    raise AssertionError("no child may be launched")


def orchestrate(run_id, session_factory, **kwargs):
    out = io.StringIO()
    kwargs.setdefault("config", agent_settings())
    kwargs.setdefault("model_factory", lambda config: ScriptedModelClient([]))
    kwargs.setdefault("image_id", IMAGE_ID)
    kwargs.setdefault("child_command", never_called)
    code = orchestrator.orchestrate_run(run_id, session_factory=session_factory, out=out, **kwargs)
    return code, out.getvalue()


def orchestration_count(session_factory) -> int:
    with session_factory() as db:
        return db.execute(text("SELECT COUNT(*) FROM orchestrations")).scalar()


# --- The claim itself ----------------------------------------------------------


def test_the_claim_reserves_every_slot_in_one_commit(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings, attempts=3)
    with session_factory() as db:
        orchestration = claim(db, run_id, requested=3)
        assert orchestration is not None
        attempts = repository.list_attempts_for_run(db, run_id)
    assert [a.attempt_index for a in attempts] == [1, 2, 3]
    assert {a.status for a in attempts} == {ATTEMPT_STATUS_QUEUED}
    assert all(a.started_at is None for a in attempts)
    assert all(a.commit_sha == COMMIT_SHA for a in attempts)
    assert [a.emphasis_key for a in attempts] == [k for k, _ in agent.INVESTIGATION_EMPHASES]


def test_a_second_orchestration_is_refused(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        assert claim(db, run_id) is not None
    with session_factory() as db:
        assert claim(db, run_id) is None
        assert len(repository.list_attempts_for_run(db, run_id)) == 2

    code, output = orchestrate(run_id, session_factory)
    assert code == worker.EXIT_NOT_CLAIMED
    assert "already been orchestrated" in output


def test_duplicate_orchestrators_launch_no_duplicate_work(
    client, session_factory, worker_settings, database_url, tmp_path
):
    """Two orchestrators, independent engines, one barrier: one reserves, one launches nothing."""
    run_id = inspected_run(client, session_factory, worker_settings, attempts=2)
    barrier = threading.Barrier(2)
    launches: list[list[str]] = [[], []]

    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n")
    fake_docker.chmod(0o755)
    scenario = tmp_path / "scenario.json"
    workdir = tmp_path / "work"
    workdir.mkdir()
    import json
    scenario.write_text(json.dumps({
        "workdir": str(workdir), "image_id": IMAGE_ID,
        "attempts": {"1": {"patch": "correct"}, "2": {"patch": "ineffective", "runner": "failing"}},
    }))

    def contender(index: int) -> int:
        engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def command(spec):
            launches[index].append(spec.attempt_id)
            return [
                __import__("sys").executable, "-m", "tests.orchestration_child",
                "--attempt-id", spec.attempt_id, "--orchestration-id", spec.orchestration_id,
                "--scenario", str(scenario),
            ]

        barrier.wait(timeout=10)
        return orchestrator.orchestrate_run(
            run_id,
            session_factory=factory,
            model_factory=lambda config: ScriptedModelClient([]),
            config=agent_settings(database_url=database_url, verify_docker_binary=str(fake_docker)),
            image_id=IMAGE_ID,
            child_command=command,
            out=io.StringIO(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(contender, [0, 1]))

    assert sorted(codes) == [worker.EXIT_OK, worker.EXIT_NOT_CLAIMED], codes
    loser = codes.index(worker.EXIT_NOT_CLAIMED)
    winner = 1 - loser
    assert launches[loser] == [], "the losing orchestrator must launch nothing"
    assert len(launches[winner]) == 2
    assert orchestration_count(session_factory) == 1
    with session_factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM patch_attempts")).scalar() == 2


# --- Legacy attempts and preconditions -------------------------------------------


def test_legacy_attempts_are_refused_and_left_untouched(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    assert worker.propose_patch(
        run_id, session_factory=session_factory, client_factory=FakeGitHub().client_factory,
        model_factory=model.factory, config=agent_settings(), out=io.StringIO(),
    ) == worker.EXIT_OK
    with session_factory() as db:
        before = [(a.id, a.status, a.diff) for a in repository.list_attempts_for_run(db, run_id)]

    code, output = orchestrate(run_id, session_factory)
    assert code == worker.EXIT_LEGACY_ATTEMPTS
    assert "manual patch attempt" in output
    assert orchestration_count(session_factory) == 0
    with session_factory() as db:
        after = [(a.id, a.status, a.diff) for a in repository.list_attempts_for_run(db, run_id)]
    assert after == before


def test_a_manual_propose_after_orchestration_is_refused(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        claim(db, run_id)
    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    out = io.StringIO()
    code = worker.propose_patch(
        run_id, session_factory=session_factory, client_factory=FakeGitHub().client_factory,
        model_factory=model.factory, config=agent_settings(), out=out,
    )
    assert code == worker.EXIT_NOT_CLAIMED
    assert "orchestrate" in out.getvalue()
    assert model.calls == 0


def test_a_run_that_is_not_ready_reserves_nothing(client, session_factory):
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    code, output = orchestrate(run_id, session_factory)
    assert code == worker.EXIT_NOT_READY
    assert "does not inspect" in output
    assert orchestration_count(session_factory) == 0


def test_unknown_run(session_factory):
    code, _ = orchestrate("00000000-0000-0000-0000-000000000000", session_factory)
    assert code == worker.EXIT_RUN_NOT_FOUND


def test_missing_model_configuration_reserves_nothing(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)

    def unconfigured(config):
        raise ModelNotConfigured("ANTHROPIC_API_KEY is not set.")

    code, output = orchestrate(run_id, session_factory, model_factory=unconfigured)
    assert code == worker.EXIT_NOT_CONFIGURED
    assert "Nothing was reserved" in output
    assert orchestration_count(session_factory) == 0
    with session_factory() as db:
        assert repository.list_attempts_for_run(db, run_id) == []


def test_docker_unavailability_reserves_nothing(client, session_factory, worker_settings, monkeypatch):
    run_id = inspected_run(client, session_factory, worker_settings)

    def unavailable(image_ref, *, limits):
        raise runner.DockerUnavailable("The Docker daemon is not reachable.")

    monkeypatch.setattr(runner, "resolve_image_id", unavailable)
    code, output = orchestrate(run_id, session_factory, image_id=None)
    assert code == worker.EXIT_DOCKER_UNAVAILABLE
    assert orchestration_count(session_factory) == 0


# --- Claiming a reserved slot by ID ----------------------------------------------


def test_a_queued_attempt_is_claimed_once_and_only_by_its_orchestration(
    client, session_factory, worker_settings
):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        orchestration = claim(db, run_id)
        first = repository.list_attempts_for_run(db, run_id)[0]
    with session_factory() as db:
        assert not repository.claim_queued_attempt(
            db, attempt_id=first.id, orchestration_id="someone-else", model="m"
        )
        assert repository.claim_queued_attempt(
            db, attempt_id=first.id, orchestration_id=orchestration.id, model="m"
        )
        assert not repository.claim_queued_attempt(
            db, attempt_id=first.id, orchestration_id=orchestration.id, model="m"
        )


def test_the_child_command_validates_attempt_ownership_before_claiming(
    client, session_factory, worker_settings
):
    """The hidden subcommand is callable by anyone; its arguments are checked."""
    first_run = inspected_run(client, session_factory, worker_settings)
    second_run = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        claim(db, first_run)
        other = claim(db, second_run)
        attempt = repository.list_attempts_for_run(db, first_run)[0]

    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    out = io.StringIO()
    code = worker.run_attempt_pipeline(
        attempt.id, other.id, session_factory=session_factory,
        client_factory=FakeGitHub().client_factory, model_factory=model.factory,
        config=agent_settings(), out=out,
    )
    assert code == worker.EXIT_NOT_CLAIMED
    assert "does not belong" in out.getvalue()
    assert model.calls == 0
    with session_factory() as db:
        assert repository.get_patch_attempt(db, attempt.id).status == ATTEMPT_STATUS_QUEUED


# --- Frozen execution settings -----------------------------------------------------


def test_frozen_settings_exclude_credentials():
    frozen = agent_settings(anthropic_api_key="sk-ant-SENTINEL-frozen").frozen_execution_config()
    assert "anthropic_api_key" not in frozen
    assert "SENTINEL" not in repr(frozen)
    assert {"anthropic_model", "agent_max_turns", "verify_test_timeout_seconds"} <= set(frozen)


def test_children_use_the_orchestrations_frozen_budgets_not_their_own(
    client, session_factory, worker_settings
):
    """The child's own settings allow 8 turns; the orchestration froze 1."""
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        orchestration = claim(db, run_id, config=agent_settings(agent_max_turns=1))
        attempt = repository.list_attempts_for_run(db, run_id)[0]
        assert orchestration.execution_config["agent_max_turns"] == 1

    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    code = worker.propose_attempt(
        attempt.id, orchestration.id, session_factory=session_factory,
        client_factory=FakeGitHub(files={"a.py": "1\n"}).client_factory,
        model_factory=model.factory, config=agent_settings(agent_max_turns=8), out=io.StringIO(),
    )
    assert code == worker.EXIT_ATTEMPT_FAILED
    with session_factory() as db:
        assert repository.get_patch_attempt(db, attempt.id).error_kind == "turn_budget_exceeded"


def test_a_child_with_a_different_model_refuses_to_run(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        orchestration = claim(db, run_id)
        attempt = repository.list_attempts_for_run(db, run_id)[0]

    class OtherModel(ScriptedModelClient):
        @property
        def model(self) -> str:
            return "some-other-model"

    model = OtherModel([make_turn(tool_calls=[submit_call()])])
    code = worker.propose_attempt(
        attempt.id, orchestration.id, session_factory=session_factory,
        client_factory=FakeGitHub().client_factory, model_factory=lambda c: model,
        config=agent_settings(), out=io.StringIO(),
    )
    assert code == worker.EXIT_NOT_CONFIGURED
    assert model.calls == 0
    with session_factory() as db:
        assert repository.get_patch_attempt(db, attempt.id).status == ATTEMPT_STATUS_QUEUED


# --- Run-level commands with several attempts ---------------------------------------


def test_verify_by_run_is_refused_when_ambiguous(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        claim(db, run_id)
        ids = [a.id for a in repository.list_attempts_for_run(db, run_id)]
    out = io.StringIO()
    code = worker.verify_patch(run_id, session_factory=session_factory, config=agent_settings(), out=out)
    assert code == worker.EXIT_AMBIGUOUS
    for attempt_id in ids:
        assert f"verify --attempt-id {attempt_id}" in out.getvalue()


def test_get_patch_attempt_for_run_raises_rather_than_guessing(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        claim(db, run_id)
        with pytest.raises(repository.AmbiguousAttempt):
            repository.get_patch_attempt_for_run(db, run_id)


# --- Emphasis ------------------------------------------------------------------------


def test_the_emphasis_is_appended_and_nothing_else_changes(client, session_factory, worker_settings):
    from app.schemas import InspectionReport

    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        report = InspectionReport.model_validate(repository.get_inspection_for_run(db, run_id).report)
    base = agent.build_initial_prompt("Issue text.", report)
    for key, emphasis in agent.INVESTIGATION_EMPHASES:
        prompt = agent.build_initial_prompt("Issue text.", report, emphasis)
        assert prompt.startswith(base), key
        assert prompt[len(base):] == f"\n\n{agent.EMPHASIS_HEADING}\n\n{emphasis}"


def test_a_manual_attempt_stays_succeeded_with_no_orchestration(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    worker.propose_patch(
        run_id, session_factory=session_factory, client_factory=FakeGitHub().client_factory,
        model_factory=model.factory, config=agent_settings(), out=io.StringIO(),
    )
    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, run_id)
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert attempt.attempt_index == 1 and attempt.orchestration_id is None
    # The first prompt is exactly milestone 3's: no emphasis section.
    assert agent.EMPHASIS_HEADING not in model.seen_messages[0][0]["content"]
