"""Worker behaviour against a mocked GitHub API.

Mocked at the transport layer, so real URL construction, response-byte caps, and
status handling all run. No network access.
"""

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import repository, worker
from app.config import Settings
from app.models import RUN_STATUS_FAILED, RUN_STATUS_READY
from tests.conftest import INSPECTABLE_PAYLOAD, FakeGitHub, big_json_tree


@pytest.fixture
def run_id(client: TestClient) -> str:
    return client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]


def run_worker(
    run_id: str,
    session_factory: sessionmaker[Session],
    fake: FakeGitHub,
    config: Settings,
) -> int:
    return worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=fake.client_factory,
        config=config,
    )


# --- Success ----------------------------------------------------------------


def test_successful_inspection_persists_report_and_ready_status(
    run_id, session_factory, fake_github, worker_settings
):
    assert run_worker(run_id, session_factory, fake_github, worker_settings) == worker.EXIT_OK

    with session_factory() as db:
        run = repository.get_run(db, run_id)
        inspection = repository.get_inspection_for_run(db, run_id)

    assert run is not None and run.status == RUN_STATUS_READY
    assert inspection is not None
    assert inspection.commit_sha == fake_github.commit_sha
    assert inspection.default_branch == "main"
    assert inspection.repository_name == "itsdangerous"
    assert inspection.error_kind is None
    assert inspection.completed_at is not None

    report = inspection.report
    assert report["repository"]["commit_sha"] == fake_github.commit_sha
    assert report["readme"]["path"] == "README.md"
    assert "itsdangerous" in report["readme"]["content"]
    assert report["assessment"]["is_python_project"] is True
    assert report["assessment"]["uses_pytest"] is True
    assert any(
        "[tool.pytest" in evidence for evidence in report["assessment"]["pytest_evidence"]
    )
    assert "tests/test_signer.py" in report["assessment"]["test_locations"]


def test_only_three_base_requests_plus_one_per_file(
    run_id, session_factory, fake_github, worker_settings
):
    """3 base requests (metadata, commit, tree) + 1 per selected file."""
    run_worker(run_id, session_factory, fake_github, worker_settings)

    paths = fake_github.paths
    assert paths[0] == "/repos/o/r"
    assert paths[1] == "/repos/o/r/commits"
    assert paths[2].startswith("/repos/o/r/git/trees/")
    content_requests = [p for p in paths if p.startswith("/repos/o/r/contents/")]
    assert len(paths) == 3 + len(content_requests)


def test_requests_are_pinned_to_the_commit_sha(
    run_id, session_factory, fake_github, worker_settings
):
    """Every content read comes from one immutable commit, not a branch name."""
    run_worker(run_id, session_factory, fake_github, worker_settings)
    for path, params in fake_github.requests:
        if path.startswith("/repos/o/r/contents/"):
            assert params["ref"] == fake_github.commit_sha


def test_branch_is_passed_as_a_query_param_not_a_path_segment(
    run_id, session_factory, worker_settings
):
    """Branch names may contain slashes, so they must not shape the path."""
    fake = FakeGitHub(repo={**FakeGitHub().repo, "default_branch": "release/v2"})
    run_worker(run_id, session_factory, fake, worker_settings)

    commit_requests = [(p, q) for p, q in fake.requests if p == "/repos/o/r/commits"]
    assert commit_requests, fake.paths
    assert commit_requests[0][1]["sha"] == "release/v2"
    assert not any("release" in path for path in fake.paths)


# --- Failure modes ----------------------------------------------------------


def _assert_failed(session_factory, run_id, expected_kind, *, contains=None):
    with session_factory() as db:
        run = repository.get_run(db, run_id)
        inspection = repository.get_inspection_for_run(db, run_id)
    assert run is not None and run.status == RUN_STATUS_FAILED
    # Status and reason are stored together, never one without the other.
    assert inspection is not None
    assert inspection.error_kind == expected_kind
    assert inspection.error_message
    if contains:
        assert contains.lower() in inspection.error_message.lower()
    return inspection


def test_repository_unavailable(run_id, session_factory, worker_settings):
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(404, json={"message": "Not Found"})})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "repository_unavailable", contains="not found")


def test_private_repository_is_refused(run_id, session_factory, worker_settings):
    fake = FakeGitHub(repo={**FakeGitHub().repo, "private": True})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "repository_unavailable", contains="private")


def test_primary_rate_limit(run_id, session_factory, worker_settings):
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1788931969"},
        json={"message": "API rate limit exceeded"})})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "rate_limited", contains="rate limit")
    # No retry storm: exactly one attempt was made.
    assert fake.paths == ["/repos/o/r"]


def test_secondary_rate_limit_reports_retry_after(run_id, session_factory, worker_settings):
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(
        429, headers={"retry-after": "60"}, json={"message": "secondary rate limit"})})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "rate_limited", contains="60")
    assert len(fake.paths) == 1


def test_timeout(run_id, session_factory, worker_settings):
    def timeout_handler(request):
        raise httpx2.TimeoutException("too slow", request=request)

    class TimingOutGitHub(FakeGitHub):
        def transport(self):
            return httpx2.MockTransport(timeout_handler)

    fake = TimingOutGitHub()
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "timeout", contains="timed out")


def test_malformed_upstream_json(run_id, session_factory, worker_settings):
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(
        200, content=b"<html>not json</html>", headers={"content-type": "text/html"})})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "upstream_protocol_error")


def test_invalid_commit_sha_is_rejected(run_id, session_factory, worker_settings):
    fake = FakeGitHub(commit_sha="not-a-sha")
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "upstream_protocol_error", contains="sha")


def test_redirect_is_not_followed(run_id, session_factory, worker_settings):
    fake = FakeGitHub(responses={"/repos/o/r": httpx2.Response(
        302, headers={"location": "https://evil.example/repos/o/r"})})
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "unexpected_redirect")
    # The redirect target was never requested.
    assert fake.paths == ["/repos/o/r"]


# --- Bounding ---------------------------------------------------------------


def test_oversized_response_body_is_rejected_before_json_parsing(
    run_id, session_factory, worker_settings
):
    """A declared Content-Length over the cap is refused without buffering."""
    payload = big_json_tree(5_000)
    tiny_cap = worker_settings.model_copy(update={"github_max_response_bytes": 10_000})
    fake = FakeGitHub(responses={"/repos/o/r/git/trees/": httpx2.Response(
        200, content=payload, headers={"content-type": "application/json"})})

    assert run_worker(run_id, session_factory, fake, tiny_cap) == worker.EXIT_INSPECTION_FAILED
    inspection = _assert_failed(session_factory, run_id, "response_too_large")
    assert "limit" in inspection.error_message.lower()


def test_oversized_file_is_omitted_without_being_fetched(
    run_id, session_factory, worker_settings
):
    """The tree's own size is the first gate, so no request is spent."""
    fake = FakeGitHub(tree=[
        {"path": "README.md", "type": "blob", "size": 5_000_000},
        {"path": "pyproject.toml", "type": "blob", "size": 60},
        {"path": "app.py", "type": "blob", "size": 10},
    ])
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_OK

    with session_factory() as db:
        report = repository.get_inspection_for_run(db, run_id).report

    assert report["readme"]["omitted"] is True
    assert "exceeds" in report["readme"]["omitted_reason"]
    assert report["readme"]["content"] is None
    # Crucially, no content request was issued for it.
    assert "/repos/o/r/contents/README.md" not in fake.paths
    assert any("README.md" in note for note in report["truncation"]["omitted_files"])


def test_truncated_tree_is_reported(run_id, session_factory, fake_github, worker_settings):
    fake_github.truncated = True
    assert run_worker(run_id, session_factory, fake_github, worker_settings) == worker.EXIT_OK

    with session_factory() as db:
        inspection = repository.get_inspection_for_run(db, run_id)

    assert inspection.tree_truncated is True
    assert inspection.report["truncation"]["tree_truncated"] is True
    assert any("truncated" in n.lower() for n in inspection.report["truncation"]["notes"])


def test_file_listing_limit_is_display_only(run_id, session_factory, worker_settings):
    """A late-sorting README must still be selected despite listing truncation."""
    tree = [{"path": f"aaa/{i:04d}.py", "type": "blob", "size": 10} for i in range(50)]
    tree += [{"path": "README.md", "type": "blob", "size": 30},
             {"path": "pyproject.toml", "type": "blob", "size": 40}]
    small = worker_settings.model_copy(update={"github_max_files_listed": 5})
    fake = FakeGitHub(tree=tree)

    assert run_worker(run_id, session_factory, fake, small) == worker.EXIT_OK
    with session_factory() as db:
        report = repository.get_inspection_for_run(db, run_id).report

    assert report["budgets"]["files_listed"] == 5
    assert report["truncation"]["file_listing_truncated"] is True
    # README sorts after the 50 aaa/* files but was still chosen and fetched.
    assert report["readme"]["path"] == "README.md"
    assert report["readme"]["content"] is not None


def test_request_budget_is_enforced(run_id, session_factory, worker_settings):
    capped = worker_settings.model_copy(update={"github_max_requests": 2})
    fake = FakeGitHub()
    assert run_worker(run_id, session_factory, fake, capped) == worker.EXIT_INSPECTION_FAILED
    _assert_failed(session_factory, run_id, "request_budget_exceeded")
    assert len(fake.paths) == 2


def test_binary_file_is_not_stored_as_text(run_id, session_factory, worker_settings):
    class BinaryGitHub(FakeGitHub):
        def handler(self, request):
            if request.url.path == "/repos/o/r/contents/README.md":
                self.requests.append((request.url.path, dict(request.url.params)))
                import base64
                return httpx2.Response(200, json={
                    "type": "file", "size": 4, "encoding": "base64",
                    "content": base64.b64encode(b"\xff\xfe\x00\x01").decode()})
            return super().handler(request)

    fake = BinaryGitHub()
    assert run_worker(run_id, session_factory, fake, worker_settings) == worker.EXIT_OK
    with session_factory() as db:
        report = repository.get_inspection_for_run(db, run_id).report
    assert report["readme"]["is_binary"] is True
    assert report["readme"]["content"] is None


# --- Worker preconditions ---------------------------------------------------


def test_unknown_run_id_exits_without_contacting_github(session_factory, fake_github, worker_settings):
    code = run_worker("00000000-0000-0000-0000-000000000000", session_factory, fake_github, worker_settings)
    assert code == worker.EXIT_RUN_NOT_FOUND
    assert fake_github.requests == []


def test_already_inspected_run_is_not_reinspected(
    run_id, session_factory, fake_github, worker_settings
):
    assert run_worker(run_id, session_factory, fake_github, worker_settings) == worker.EXIT_OK
    second = FakeGitHub()
    assert run_worker(run_id, session_factory, second, worker_settings) == worker.EXIT_NOT_CLAIMED
    assert second.requests == []
