"""The progress endpoint stays small, and says what a poll needs.

The point of a separate endpoint is that polling must not re-download every diff,
container log, and event every couple of seconds. That is easy to assert weakly
(a schema without those fields) and easy to regress (someone adds `verification`
as a full nested model). So the size test here builds a deliberately *fat* run —
three attempts, 60 KB diffs, 200 KB logs, a full comparison — and asserts on the
real serialized bytes.
"""

from __future__ import annotations

import json

from app import agent, repository
from app.models import (
    ATTEMPT_STATUS_SUCCEEDED,
    JOB_STATUS_QUEUED,
    ORCH_STATUS_RUNNING,
    Verification,
)
from tests.conftest import COMMIT_SHA, INSPECTABLE_PAYLOAD

BIG_DIFF_MARKER = "ZZDIFFMARKERZZ"
BIG_LOG_MARKER = "ZZLOGMARKERZZ"


def fat_run(client, db, *, attempts: int = 3) -> str:
    """A run carrying about as much payload as this application can produce."""
    payload = dict(INSPECTABLE_PAYLOAD, max_parallel_attempts=attempts)
    run_id = client.post("/api/runs", json=payload).json()["id"]

    repository.save_inspection_success(
        db,
        run_id=run_id,
        commit_sha=COMMIT_SHA,
        default_branch="main",
        repository_name="r",
        repository_description="d",
        tree_truncated=False,
        report={
            "repository": {},
            "files": [{"path": f"f{i}.py"} for i in range(400)],
            "readme": None,
            "python_config_files": [],
            "assessment": {
                "is_python_project": True,
                "uses_pytest": True,
                "python_evidence": [],
                "pytest_evidence": [],
                "test_locations": [],
                "caveat": "filename heuristic",
            },
            "budgets": {
                "requests_made": 5, "max_requests": 20, "files_in_tree": 400,
                "files_listed": 400, "max_files_listed": 500, "files_fetched": 2,
                "max_files_fetched": 8, "content_bytes_stored": 100,
                "max_total_content_bytes": 400_000, "max_file_bytes": 100_000,
            },
            "truncation": {},
        },
        started_at=repository.utcnow(),
    )

    orchestration = repository.claim_orchestration(
        db,
        run_id=run_id,
        requested_attempts=attempts,
        concurrency_limit=attempts,
        effective_concurrency=attempts,
        model="m",
        commit_sha=COMMIT_SHA,
        profile="python-pytest",
        image_ref="img:1",
        image_id="sha256:abc",
        workspace_root="/tmp/ws",
        execution_config={},
        emphases=[agent.emphasis_for(i) for i in range(1, attempts + 1)],
    )
    assert orchestration is not None
    # A queued orchestration has not started; move it on so the progress response
    # is exercised against a run that is actually under way.
    repository.start_orchestration(db, orchestration.id)

    big_diff = BIG_DIFF_MARKER + ("x" * 60_000)
    big_log = BIG_LOG_MARKER + ("y" * 200_000)

    for attempt in repository.list_attempts_for_run(db, run_id):
        assert repository.claim_queued_attempt(
            db, attempt_id=attempt.id, orchestration_id=orchestration.id, model="m"
        )
        repository.save_attempt_success(
            db,
            attempt_id=attempt.id,
            diff=big_diff,
            summary="s" * 4_000,
            suggested_test_command="pytest",
            input_tokens=10,
            output_tokens=20,
            max_summary_chars=4_000,
            max_command_chars=500,
        )
        for seq in range(1, 40):
            repository.record_attempt_event(
                db,
                attempt_id=attempt.id,
                kind="file_read",
                summary=f"Read file {seq}",
                detail="d" * 2_000,
                max_events=200,
                max_detail_chars=2_000,
            )
        db.add(
            Verification(
                attempt_id=attempt.id,
                status="completed",
                outcome="fix_demonstrated",
                commit_sha=COMMIT_SHA,
                patch_sha256="a" * 64,
                profile="python-pytest",
                image_ref="img:1",
                image_id="sha256:abc",
                patch_applied=True,
                patch_touched_tests=False,
                baseline_log=big_log,
                patched_log=big_log,
                supplemental_log=big_log,
                comparison={"fixed": [f"t{i}" for i in range(200)], "outcome": "fix_demonstrated"},
                baseline_summary={"collected": [f"t{i}" for i in range(200)]},
                patched_summary={"collected": [f"t{i}" for i in range(200)]},
            )
        )
        db.commit()
    return run_id


def test_progress_reports_run_job_and_attempt_state(client, db) -> None:
    run_id = fat_run(client, db, attempts=2)
    # Enqueued directly: this run already has an orchestration, so `POST /start`
    # would correctly refuse it with a 409. The job is what a run started before
    # its orchestration existed would look like.
    repository.enqueue_execution_job(db, run_id=run_id)

    body = client.get(f"/api/runs/{run_id}/progress").json()

    assert body["id"] == run_id
    assert body["status"] == "ready"
    assert body["job"]["status"] == JOB_STATUS_QUEUED
    assert body["orchestration"]["status"] == ORCH_STATUS_RUNNING
    assert [a["attempt_index"] for a in body["attempts"]] == [1, 2]
    for attempt in body["attempts"]:
        assert attempt["status"] == ATTEMPT_STATUS_SUCCEEDED
        assert attempt["verification"]["status"] == "completed"
        assert attempt["verification"]["outcome"] == "fix_demonstrated"
        # The count is reported; the event bodies are not.
        assert attempt["events_total"] == 39


def test_progress_stays_small_next_to_the_detail_payload(client, db) -> None:
    run_id = fat_run(client, db, attempts=3)
    # Enqueued directly: this run already has an orchestration, so `POST /start`
    # would correctly refuse it with a 409. The job is what a run started before
    # its orchestration existed would look like.
    repository.enqueue_execution_job(db, run_id=run_id)

    progress = client.get(f"/api/runs/{run_id}/progress")
    detail = client.get(f"/api/runs/{run_id}")

    progress_bytes = len(progress.content)
    detail_bytes = len(detail.content)

    # The detail payload really is enormous for this run — that is the point.
    assert detail_bytes > 1_000_000, detail_bytes
    assert progress_bytes < 4_000, progress_bytes
    assert detail_bytes > progress_bytes * 100

    text = progress.text
    assert BIG_DIFF_MARKER not in text, "a diff reached the progress payload"
    assert BIG_LOG_MARKER not in text, "a container log reached the progress payload"

    payload = json.loads(text)
    assert "inspection" not in payload, "the inspection report must not be polled"
    for attempt in payload["attempts"]:
        for forbidden in ("diff", "summary", "events", "suggested_test_command"):
            assert forbidden not in attempt, f"{forbidden} reached the progress payload"
        verification = attempt["verification"]
        for forbidden in ("baseline_log", "patched_log", "comparison", "runner_args"):
            assert forbidden not in verification, f"{forbidden} reached the progress payload"


def test_progress_for_an_unstarted_run_has_no_job(client) -> None:
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    body = client.get(f"/api/runs/{run_id}/progress").json()

    assert body["job"] is None
    assert body["orchestration"] is None
    assert body["attempts"] == []
    assert body["status"] == "pending"


def test_progress_for_an_unknown_run_is_404(client) -> None:
    response = client.get("/api/runs/00000000-0000-0000-0000-000000000000/progress")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
