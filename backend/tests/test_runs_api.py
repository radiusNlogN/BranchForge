"""API behaviour: creating, retrieving, listing, validation, and 404s."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import repository, worker
from tests.conftest import INSPECTABLE_PAYLOAD, VALID_PAYLOAD


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_then_retrieve_persisted_run(client: TestClient) -> None:
    created = client.post("/api/runs", json=VALID_PAYLOAD)
    assert created.status_code == 201, created.text
    body = created.json()

    # The server assigns the identity, status and timestamps.
    assert len(body["id"]) == 36
    assert body["status"] == "pending"
    assert body["repository_url"] == "https://github.com/octocat/Hello-World"
    assert body["issue_description"] == "Sorting breaks on empty input."
    assert body["max_parallel_attempts"] == 2

    fetched = client.get(f"/api/runs/{body['id']}")
    assert fetched.status_code == 200
    detail = fetched.json()

    # The detail endpoint returns the same run plus its inspection, which is null
    # until the worker has run.
    assert detail["inspection"] is None
    assert {key: detail[key] for key in body} == body


def test_created_run_is_visible_in_a_separate_session(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The row is committed, not merely held in the request's session."""
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["id"]

    with session_factory() as session:
        stored = repository.get_run(session, run_id)
        assert stored is not None
        assert stored.repository_url == "https://github.com/octocat/Hello-World"
        assert stored.status == "pending"


def test_repository_url_is_normalized_before_storage(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={**VALID_PAYLOAD, "repository_url": "https://github.com/octocat/Hello-World.git/"},
    )
    assert response.status_code == 201
    assert response.json()["repository_url"] == "https://github.com/octocat/Hello-World"


def test_issue_description_is_trimmed(client: TestClient) -> None:
    response = client.post(
        "/api/runs",
        json={**VALID_PAYLOAD, "issue_description": "  padded description  "},
    )
    assert response.status_code == 201
    assert response.json()["issue_description"] == "padded description"


def test_max_parallel_attempts_defaults_to_one(client: TestClient) -> None:
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "max_parallel_attempts"}
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201
    assert response.json()["max_parallel_attempts"] == 1


# --- Timestamps -------------------------------------------------------------


def test_timestamps_survive_the_round_trip_as_the_same_instant(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Guards against a naive datetime being read back as local time.

    The stored instant must fall inside the wall-clock window around the request.
    On any machine whose local timezone is not UTC, mistaking a naive UTC value
    for local time pushes it outside this window.
    """
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    response = client.post("/api/runs", json=VALID_PAYLOAD)
    after = datetime.now(timezone.utc) + timedelta(seconds=1)

    body = response.json()
    created_at = parse_timestamp(body["created_at"])

    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timedelta(0)
    assert before <= created_at <= after

    # The same instant is reported by a subsequent read...
    refetched = parse_timestamp(client.get(f"/api/runs/{body['id']}").json()["created_at"])
    assert refetched == created_at

    # ...and matches what is actually in the database.
    with session_factory() as session:
        stored = repository.get_run(session, body["id"])
        assert stored is not None
        stored_utc = stored.created_at
        if stored_utc.tzinfo is None:
            stored_utc = stored_utc.replace(tzinfo=timezone.utc)
        assert stored_utc == created_at


# --- Invalid input ----------------------------------------------------------


@pytest.mark.parametrize(
    "repository_url",
    [
        "http://github.com/octocat/Hello-World",
        "https://gitlab.com/octocat/Hello-World",
        "https://github.com.evil.com/octocat/Hello-World",
        "https://user:pass@github.com/octocat/Hello-World",
        "https://github.com/octocat",
        "https://github.com/octocat/Hello-World?tab=readme",
        "https://github.com/octocat/Hello-World#frag",
        "git@github.com:octocat/Hello-World.git",
        "not a url",
    ],
)
def test_rejects_invalid_repository_url(client: TestClient, repository_url: str) -> None:
    response = client.post("/api/runs", json={**VALID_PAYLOAD, "repository_url": repository_url})
    assert response.status_code == 422
    assert any(
        error["loc"][-1] == "repository_url" for error in response.json()["detail"]
    ), response.text


@pytest.mark.parametrize("issue_description", ["", "   ", "\n\t  \n"])
def test_rejects_blank_issue_description(client: TestClient, issue_description: str) -> None:
    response = client.post(
        "/api/runs", json={**VALID_PAYLOAD, "issue_description": issue_description}
    )
    assert response.status_code == 422


def test_rejects_overlong_issue_description(client: TestClient) -> None:
    response = client.post(
        "/api/runs", json={**VALID_PAYLOAD, "issue_description": "x" * 10_001}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("attempts", [0, -1, 4, 10])
def test_rejects_out_of_range_attempts(client: TestClient, attempts: int) -> None:
    response = client.post(
        "/api/runs", json={**VALID_PAYLOAD, "max_parallel_attempts": attempts}
    )
    assert response.status_code == 422


def test_rejects_missing_fields(client: TestClient) -> None:
    assert client.post("/api/runs", json={}).status_code == 422


def test_rejects_client_supplied_id_and_status(client: TestClient) -> None:
    """Identity and status are server-owned; extra fields are refused outright."""
    response = client.post(
        "/api/runs", json={**VALID_PAYLOAD, "id": "abc", "status": "completed"}
    )
    assert response.status_code == 422


# --- Retrieval failures -----------------------------------------------------


def test_unknown_run_id_returns_404(client: TestClient) -> None:
    response = client.get("/api/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert "detail" in response.json()


def test_malformed_run_id_returns_404(client: TestClient) -> None:
    assert client.get("/api/runs/not-a-uuid").status_code == 404


# --- Listing ----------------------------------------------------------------


def test_list_is_empty_initially(client: TestClient) -> None:
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_newest_first(client: TestClient) -> None:
    ids = [
        client.post(
            "/api/runs",
            json={**VALID_PAYLOAD, "issue_description": f"issue {index}"},
        ).json()["id"]
        for index in range(5)
    ]

    listed = [run["id"] for run in client.get("/api/runs").json()]
    assert listed == list(reversed(ids))


def test_list_respects_limit(client: TestClient) -> None:
    for index in range(5):
        client.post("/api/runs", json={**VALID_PAYLOAD, "issue_description": f"issue {index}"})

    assert len(client.get("/api/runs", params={"limit": 2}).json()) == 2


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_list_rejects_out_of_bounds_limit(client: TestClient, limit: int) -> None:
    assert client.get("/api/runs", params={"limit": limit}).status_code == 422


# --- Inspection exposed through run detail -----------------------------------


def test_run_detail_has_null_inspection_before_the_worker_runs(client: TestClient) -> None:
    run_id = client.post("/api/runs", json=VALID_PAYLOAD).json()["id"]
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "pending"
    assert body["inspection"] is None


def test_run_detail_returns_the_persisted_inspection(
    client: TestClient, session_factory, fake_github, worker_settings
) -> None:
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    assert (
        worker.inspect_run(
            run_id,
            session_factory=session_factory,
            client_factory=fake_github.client_factory,
            config=worker_settings,
        )
        == worker.EXIT_OK
    )

    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "ready"

    inspection = body["inspection"]
    assert inspection is not None
    assert inspection["run_id"] == run_id
    assert inspection["commit_sha"] == fake_github.commit_sha
    assert inspection["default_branch"] == "main"
    assert inspection["error_kind"] is None
    # Timestamps keep the milestone-1 UTC contract.
    assert inspection["started_at"].endswith("Z")
    assert inspection["completed_at"].endswith("Z")

    report = inspection["report"]
    assert report["repository"]["commit_sha"] == fake_github.commit_sha
    assert report["readme"]["path"] == "README.md"
    assert report["assessment"]["uses_pytest"] is True
    assert report["assessment"]["caveat"]
    assert report["budgets"]["requests_made"] >= 3


def test_failed_inspection_is_visible_with_its_error(
    client: TestClient, session_factory, worker_settings
) -> None:
    import httpx2

    from tests.conftest import FakeGitHub

    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(404, json={"message": "Not Found"})})
    worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=fake.client_factory,
        config=worker_settings,
    )

    body = client.get(f"/api/runs/{run_id}").json()
    assert body["status"] == "failed"
    assert body["inspection"]["error_kind"] == "repository_unavailable"
    assert body["inspection"]["error_message"]
    assert body["inspection"]["report"] is None


def test_list_endpoint_does_not_include_inspections(
    client: TestClient, session_factory, fake_github, worker_settings
) -> None:
    """Inspections would make the list response unbounded."""
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=fake_github.client_factory,
        config=worker_settings,
    )

    listed = client.get("/api/runs").json()
    assert listed[0]["id"] == run_id
    assert listed[0]["status"] == "ready"
    assert "inspection" not in listed[0]
