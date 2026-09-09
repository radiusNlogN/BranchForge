"""Attempt claiming, and the guarantee that credentials never leak.

Claim contention uses two independent engines against one temporary file-backed
database, started together with a barrier — the same shape as the milestone-2
inspection contention test.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import repository, worker
from app.models import ATTEMPT_STATUS_SUCCEEDED, RUN_STATUS_READY
from tests.conftest import (
    INSPECTABLE_PAYLOAD,
    FakeGitHub,
    ScriptedModelClient,
    agent_settings,
    make_turn,
    read_call,
    submit_call,
)

# A fake credential used only to prove nothing persists it. Never a real key.
SENTINEL_KEY = "sk-ant-SENTINEL-DO-NOT-PERSIST-4f3a9c17"


def independent_session_factory(database_url: str) -> sessionmaker:
    engine = create_engine(
        database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def inspected_run(client: TestClient, session_factory, worker_settings) -> str:
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=FakeGitHub().client_factory,
        config=worker_settings,
    )
    return run_id


def test_sequential_second_claim_is_refused(client, session_factory, worker_settings):
    run_id = inspected_run(client, session_factory, worker_settings)
    with session_factory() as db:
        first = repository.claim_patch_attempt(
            db, run_id=run_id, model="m", commit_sha="a" * 40
        )
        assert first is not None
    with session_factory() as db:
        assert repository.claim_patch_attempt(
            db, run_id=run_id, model="m", commit_sha="a" * 40
        ) is None


def test_concurrent_attempts_only_one_calls_the_model(
    client, session_factory, database_url, worker_settings
):
    """The losing worker must make zero model calls and zero GitHub requests."""
    run_id = inspected_run(client, session_factory, worker_settings)

    workers = [
        (
            independent_session_factory(database_url),
            ScriptedModelClient([make_turn(tool_calls=[submit_call()])]),
            FakeGitHub(),
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(len(workers))

    def attempt(triple) -> int:
        factory, model, github = triple
        barrier.wait(timeout=10)
        return worker.propose_patch(
            run_id,
            session_factory=factory,
            client_factory=github.client_factory,
            model_factory=model.factory,
            config=agent_settings(),
        )

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        codes = list(pool.map(attempt, workers))

    assert sorted(codes) == [worker.EXIT_OK, worker.EXIT_NOT_CLAIMED], codes

    winners = [w for w, code in zip(workers, codes) if code == worker.EXIT_OK]
    losers = [w for w, code in zip(workers, codes) if code != worker.EXIT_OK]
    assert len(winners) == 1 and len(losers) == 1

    _, winning_model, _ = winners[0]
    _, losing_model, losing_github = losers[0]
    assert winning_model.calls >= 1, "the winner should have called the model"
    assert losing_model.calls == 0, "the loser must not call the model"
    assert losing_github.requests == [], "the loser must not contact GitHub"

    # Exactly one attempt row exists, and the run is untouched.
    with workers[0][0]() as db:
        attempt_row = repository.get_patch_attempt_for_run(db, run_id)
        assert attempt_row is not None
        assert attempt_row.status == ATTEMPT_STATUS_SUCCEEDED
        assert repository.get_run(db, run_id).status == RUN_STATUS_READY
        count = db.execute(text("SELECT COUNT(*) FROM patch_attempts")).scalar()
        assert count == 1


def test_credentials_are_never_persisted_or_returned(
    client: TestClient, session_factory, worker_settings, engine
):
    """Run an attempt with a synthetic sentinel key and prove it appears nowhere.

    The sentinel is fake, so this can assert on content directly — a real key is
    never used for a search like this.
    """
    run_id = inspected_run(client, session_factory, worker_settings)

    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    code = worker.propose_patch(
        run_id,
        session_factory=session_factory,
        client_factory=FakeGitHub(files={"a.py": "x=1\n"}).client_factory,
        model_factory=model.factory,
        config=agent_settings(anthropic_api_key=SENTINEL_KEY),
    )
    assert code == worker.EXIT_OK

    # Every text column of every table must be free of the sentinel.
    with engine.connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        ]
        for table in tables:
            rows = connection.execute(text(f"SELECT * FROM {table}")).fetchall()  # noqa: S608
            for row in rows:
                blob = " ".join(str(value) for value in row)
                assert SENTINEL_KEY not in blob, f"credential leaked into {table}"
                assert "SENTINEL" not in blob, f"credential fragment leaked into {table}"

    # Nor may it appear in the API response.
    body = client.get(f"/api/runs/{run_id}").text
    assert SENTINEL_KEY not in body and "SENTINEL" not in body
