"""Claim contention: exactly one worker may take a pending run.

The sequential test covers the ordinary case. The concurrent test uses two
independent engines against the same temporary file-backed database, started
together with a barrier, because that is the situation the conditional UPDATE
exists to survive.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import repository, worker
from app.models import RUN_STATUS_INSPECTING, RUN_STATUS_READY
from tests.conftest import INSPECTABLE_PAYLOAD, FakeGitHub


def independent_session_factory(database_url: str) -> sessionmaker:
    """A session factory with its own engine and connection pool.

    Separate engines matter: two sessions from one factory could share a pooled
    connection, which would not reproduce cross-process contention.
    """
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_sequential_duplicate_claim_only_succeeds_once(client: TestClient, session_factory):
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]

    with session_factory() as db:
        assert repository.claim_run_for_inspection(db, run_id) is True
    with session_factory() as db:
        assert repository.claim_run_for_inspection(db, run_id) is False

    with session_factory() as db:
        assert repository.get_run(db, run_id).status == RUN_STATUS_INSPECTING


def test_claiming_a_nonexistent_run_returns_false(session_factory):
    with session_factory() as db:
        assert repository.claim_run_for_inspection(db, "00000000-0000-0000-0000-000000000000") is False


def test_concurrent_claims_from_independent_connections(client: TestClient, database_url):
    """Two connections race for one pending run; exactly one wins."""
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]

    factories = [independent_session_factory(database_url) for _ in range(2)]
    barrier = threading.Barrier(len(factories))

    def attempt(factory) -> bool:
        barrier.wait(timeout=10)  # start both as close together as possible
        with factory() as db:
            return repository.claim_run_for_inspection(db, run_id)

    with ThreadPoolExecutor(max_workers=len(factories)) as pool:
        results = list(pool.map(attempt, factories))

    assert sum(results) == 1, f"expected exactly one winner, got {results}"

    with factories[0]() as db:
        assert repository.get_run(db, run_id).status == RUN_STATUS_INSPECTING


def test_concurrent_workers_only_one_contacts_github(client: TestClient, database_url, worker_settings):
    """The losing worker must exit before making any GitHub request."""
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]

    workers = [
        (independent_session_factory(database_url), FakeGitHub()),
        (independent_session_factory(database_url), FakeGitHub()),
    ]
    barrier = threading.Barrier(len(workers))

    def attempt(pair) -> int:
        factory, fake = pair
        barrier.wait(timeout=10)
        return worker.inspect_run(
            run_id,
            session_factory=factory,
            client_factory=fake.client_factory,
            config=worker_settings,
        )

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        codes = list(pool.map(attempt, workers))

    assert sorted(codes) == [worker.EXIT_OK, worker.EXIT_NOT_CLAIMED], codes

    winners = [fake for (_, fake), code in zip(workers, codes) if code == worker.EXIT_OK]
    losers = [fake for (_, fake), code in zip(workers, codes) if code != worker.EXIT_OK]

    assert len(winners) == 1 and len(losers) == 1
    assert winners[0].requests, "the winning worker should have called GitHub"
    assert losers[0].requests == [], "the losing worker must not contact GitHub"

    with workers[0][0]() as db:
        assert repository.get_run(db, run_id).status == RUN_STATUS_READY
        assert repository.get_inspection_for_run(db, run_id) is not None
