"""BranchForge inspection worker — a process separate from the API.

    uv run python -m app.worker inspect --run-id <UUID>

Uses the same DATABASE_URL and the same database layer (`app.repository`) as the
API, but imports nothing from `app.main` or `app.routers`.

Transaction discipline, which matters because SQLite is the local database:

1. A short session claims the run atomically and commits, then closes.
2. All GitHub work happens with **no** session open and no transaction held.
3. A second short session stores the outcome in a single commit.

Nothing here schedules work, retries, or recovers stuck runs. If the process is
killed mid-inspection, the run stays `inspecting` — see the README limitations.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app import agent, github_client, repository, runner, verification
from app.agent import AgentFailure
from app.config import Settings, settings
from app.database import SessionLocal
from app.github_client import ClientLimits, GitHubClient, GitHubError
from app.inspection import inspect_repository
from app.model_client import AnthropicModelClient, ModelClient, ModelNotConfigured
from app.models import ATTEMPT_STATUS_SUCCEEDED, RUN_STATUS_PENDING, RUN_STATUS_READY
from app import snapshot as snapshot_module
from app.schemas import InspectionReport
from app.time_utils import utcnow

EXIT_OK = 0
EXIT_INSPECTION_FAILED = 1
EXIT_ATTEMPT_FAILED = 1
EXIT_RUN_NOT_FOUND = 2
EXIT_NOT_CLAIMED = 3
EXIT_NOT_READY = 4
EXIT_NOT_CONFIGURED = 5
EXIT_VERIFICATION_FAILED = 1
EXIT_NO_PATCH = 4
EXIT_DOCKER_UNAVAILABLE = 6

SessionFactory = Callable[[], Session] | sessionmaker[Session]
ClientFactory = Callable[[Settings], GitHubClient]
ModelFactory = Callable[[Settings], ModelClient]


def default_client_factory(config: Settings) -> GitHubClient:
    """Build a real GitHub client from configuration."""
    http = github_client.build_client(
        base_url=config.github_api_base_url,
        user_agent=config.github_user_agent,
        timeout_seconds=config.github_request_timeout_seconds,
        connect_timeout_seconds=config.github_connect_timeout_seconds,
    )
    return GitHubClient(
        http,
        ClientLimits(
            max_requests=config.github_max_requests,
            max_response_bytes=config.github_max_response_bytes,
            max_content_response_bytes=config.github_max_content_response_bytes,
        ),
    )


def default_model_factory(config: Settings) -> ModelClient:
    """Build the real model client. Raises ModelNotConfigured if unusable."""
    return AnthropicModelClient(config)


@contextmanager
def _session(session_factory: SessionFactory) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def inspect_run(
    run_id: str,
    *,
    session_factory: SessionFactory | None = None,
    client_factory: ClientFactory | None = None,
    config: Settings | None = None,
    out: Any = sys.stdout,
) -> int:
    """Claim a pending run, inspect its repository, and persist the outcome.

    `session_factory` is injectable so tests (and any future embedding) can point
    the worker at a different database than the process-wide default.
    """
    session_factory = session_factory or SessionLocal
    client_factory = client_factory or default_client_factory
    config = config or settings

    def emit(message: str) -> None:
        print(message, file=out)

    # --- 1. Claim, in a short transaction, before any network work -----------
    with _session(session_factory) as db:
        run = repository.get_run(db, run_id)
        if run is None:
            emit(f"No run found with id {run_id!r}.")
            return EXIT_RUN_NOT_FOUND

        current_status = run.status
        repository_url = run.repository_url

        if current_status != RUN_STATUS_PENDING:
            emit(
                f"Run {run_id} is {current_status!r}, not 'pending'. Only a pending "
                f"run can be claimed, so there is nothing to do."
            )
            return EXIT_NOT_CLAIMED

        if not repository.claim_run_for_inspection(db, run_id):
            emit(
                f"Run {run_id} was claimed by another worker first. Exiting without "
                f"contacting GitHub."
            )
            return EXIT_NOT_CLAIMED

    emit(f"Claimed run {run_id} -> inspecting")
    emit(f"Inspecting {repository_url} (read-only; no clone, no code execution)")

    # --- 2. Network work, with no database session open ----------------------
    started_at = utcnow()
    client = client_factory(config)
    try:
        try:
            outcome = inspect_repository(client, repository_url, config)
        finally:
            closer = getattr(client, "_client", None)
            if closer is not None:
                closer.close()
    except GitHubError as error:
        # --- 3a. Handled failure: status and reason, one transaction ---------
        with _session(session_factory) as db:
            repository.save_inspection_failure(
                db,
                run_id=run_id,
                error_kind=error.kind,
                error_message=str(error),
                started_at=started_at,
            )
        emit(f"Inspection failed ({error.kind}): {error}")
        emit(f"Run {run_id} -> failed")
        return EXIT_INSPECTION_FAILED

    # --- 3b. Success: report and ready status in one transaction -------------
    with _session(session_factory) as db:
        repository.save_inspection_success(
            db,
            run_id=run_id,
            commit_sha=outcome.commit_sha,
            default_branch=outcome.default_branch,
            repository_name=outcome.repository_name,
            repository_description=outcome.repository_description,
            tree_truncated=outcome.tree_truncated,
            report=outcome.report.model_dump(mode="json"),
            started_at=started_at,
        )

    budgets = outcome.report.budgets
    emit(
        f"Inspected {outcome.repository_name or repository_url} at {outcome.commit_sha[:12]} "
        f"on {outcome.default_branch}"
    )
    emit(
        f"  {budgets.files_in_tree:,} files in tree, {budgets.files_listed:,} listed, "
        f"{budgets.files_fetched} previewed, {budgets.requests_made} GitHub request(s)"
    )
    assessment = outcome.report.assessment
    emit(
        f"  Python project: {assessment.is_python_project} | uses pytest: "
        f"{assessment.uses_pytest} (heuristic)"
    )
    if outcome.report.truncation.notes:
        emit("  Notes:")
        for note in outcome.report.truncation.notes:
            emit(f"    - {note}")
    emit(f"Run {run_id} -> ready (inspection complete; no fix attempted)")
    return EXIT_OK


def propose_patch(
    run_id: str,
    *,
    session_factory: SessionFactory | None = None,
    client_factory: ClientFactory | None = None,
    model_factory: ModelFactory | None = None,
    config: Settings | None = None,
    out: Any = sys.stdout,
) -> int:
    """Run one bounded agent attempt to propose a patch for an inspected run.

    Transaction discipline matches the inspect command: short sessions only, and
    no session held open across model or GitHub calls. Operational events are
    committed as they happen so the dashboard can show progress mid-attempt.

    The proposed patch is never applied and no repository code is executed.
    """
    session_factory = session_factory or SessionLocal
    client_factory = client_factory or default_client_factory
    model_factory = model_factory or default_model_factory
    config = config or settings

    def emit(message: str) -> None:
        print(message, file=out)

    # --- 1. Preconditions, in a short read-only session ----------------------
    with _session(session_factory) as db:
        run = repository.get_run(db, run_id)
        if run is None:
            emit(f"No run found with id {run_id!r}.")
            return EXIT_RUN_NOT_FOUND

        if run.status != RUN_STATUS_READY:
            emit(
                f"Run {run_id} is {run.status!r}, not 'ready'. Inspect it first:\n"
                f"  uv run python -m app.worker inspect --run-id {run_id}"
            )
            return EXIT_NOT_READY

        inspection = repository.get_inspection_for_run(db, run_id)
        if inspection is None or not inspection.report:
            emit(
                f"Run {run_id} has no inspection report to work from. Re-inspect it first."
            )
            return EXIT_NOT_READY

        repository_url = run.repository_url
        issue_description = run.issue_description
        commit_sha = inspection.commit_sha
        report = InspectionReport.model_validate(inspection.report)

    # --- 2. Model configuration BEFORE claiming ------------------------------
    # A missing key must not leave a claimed attempt behind.
    try:
        model = model_factory(config)
    except ModelNotConfigured as error:
        emit(f"Model is not configured: {error}")
        emit("No attempt was created.")
        return EXIT_NOT_CONFIGURED

    # --- 3. Claim by inserting the attempt row -------------------------------
    with _session(session_factory) as db:
        attempt = repository.claim_patch_attempt(
            db, run_id=run_id, model=model.model, commit_sha=commit_sha
        )
        if attempt is None:
            emit(
                f"Run {run_id} already has a patch attempt. One attempt per run in "
                f"this milestone; exiting without calling the model."
            )
            return EXIT_NOT_CLAIMED
        attempt_id = attempt.id

    emit(f"Claimed patch attempt for run {run_id}")
    emit(f"Model: {model.model} | commit {commit_sha}")
    emit("Proposing a patch (read-only; nothing is applied or executed)")

    # --- 4. Agent work, with no session held open ----------------------------
    def record(kind: str, summary: str, detail: str | None) -> None:
        """Commit one event immediately, in its own short transaction."""
        with _session(session_factory) as event_db:
            repository.record_attempt_event(
                event_db,
                attempt_id=attempt_id,
                kind=kind,
                summary=summary,
                detail=detail,
                max_events=config.agent_max_events,
                max_detail_chars=config.agent_max_event_detail_chars,
            )
        emit(f"  · {summary}")

    github = client_factory(config)
    try:
        try:
            result = agent.run_agent(
                model=model,
                github=github,
                repository_url=repository_url,
                issue_description=issue_description,
                report=report,
                commit_sha=commit_sha or "",
                config=config,
                record=record,
            )
        finally:
            closer = getattr(github, "_client", None)
            if closer is not None:
                closer.close()
    except AgentFailure as failure:
        with _session(session_factory) as db:
            repository.save_attempt_failure(
                db,
                attempt_id=attempt_id,
                error_kind=failure.kind,
                error_message=str(failure),
                input_tokens=None,
                output_tokens=None,
                max_error_chars=config.agent_max_error_chars,
            )
        emit(f"Attempt failed ({failure.kind}): {failure}")
        emit(f"Run {run_id} is still 'ready' — inspection is unaffected.")
        return EXIT_ATTEMPT_FAILED

    # --- 5. Patch and success status, one transaction ------------------------
    with _session(session_factory) as db:
        repository.save_attempt_success(
            db,
            attempt_id=attempt_id,
            diff=result.patch.diff,
            summary=result.patch.summary,
            suggested_test_command=result.patch.suggested_test_command,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            max_summary_chars=config.agent_max_summary_chars,
            max_command_chars=config.agent_max_test_command_chars,
        )

    emit(
        f"Proposed a patch touching {len(result.patch.files_changed)} file(s): "
        + ", ".join(result.patch.files_changed)
    )
    if result.input_tokens is not None or result.output_tokens is not None:
        emit(f"  tokens: in={result.input_tokens} out={result.output_tokens}")
    emit(f"  suggested test command: {result.patch.suggested_test_command}")
    emit("UNVERIFIED: the patch was not applied and no tests were run.")
    return EXIT_OK


def verify_patch(
    run_id: str,
    *,
    session_factory: SessionFactory | None = None,
    config: Settings | None = None,
    image_id: str | None = None,
    snapshot_fetcher: Any = None,
    test_runner: Any = None,
    out: Any = sys.stdout,
) -> int:
    """Run the proposed patch against the repository's own tests in containers.

    Ordering matters and mirrors `propose`: everything that can fail cheaply is
    checked *before* the claim, so a missing image or a stopped Docker daemon
    never leaves a claimed verification behind.

    Repository code executes only inside the container. This process orchestrates
    and never imports, installs, or runs anything from the repository.
    """
    session_factory = session_factory or SessionLocal
    config = config or settings

    def emit(message: str) -> None:
        print(message, file=out)

    # --- 1. Preconditions, in a short read-only session ----------------------
    with _session(session_factory) as db:
        run = repository.get_run(db, run_id)
        if run is None:
            emit(f"No run found with id {run_id!r}.")
            return EXIT_RUN_NOT_FOUND

        attempt = repository.get_patch_attempt_for_run(db, run_id)
        if attempt is None:
            emit(
                f"Run {run_id} has no patch attempt to verify. Propose one first:\n"
                f"  uv run python -m app.worker propose --run-id {run_id}"
            )
            return EXIT_NO_PATCH
        if attempt.status != ATTEMPT_STATUS_SUCCEEDED or not attempt.diff:
            emit(
                f"The patch attempt for run {run_id} is {attempt.status!r} with no "
                f"stored diff. There is nothing to verify."
            )
            return EXIT_NO_PATCH
        if not attempt.commit_sha:
            emit(
                f"The patch attempt for run {run_id} has no recorded commit SHA, so "
                f"the exact source it was written against cannot be retrieved."
            )
            return EXIT_NO_PATCH

        attempt_id = attempt.id
        diff = attempt.diff
        commit_sha = attempt.commit_sha
        repository_url = run.repository_url

    # --- 2. Docker and the image, BEFORE claiming ----------------------------
    limits = verification.runner_limits_from(config)
    try:
        resolved_image_id = image_id or runner.resolve_image_id(
            config.verify_image, limits=limits
        )
    except runner.DockerUnavailable as error:
        emit(f"Docker is unavailable: {error}")
        emit("No verification was created.")
        return EXIT_DOCKER_UNAVAILABLE

    # --- 3. Claim by inserting the verification row --------------------------
    with _session(session_factory) as db:
        claimed = repository.claim_verification(
            db,
            attempt_id=attempt_id,
            commit_sha=commit_sha,
            patch_sha256=verification.patch_fingerprint(diff),
            profile=config.verify_profile,
            image_ref=config.verify_image,
            image_id=resolved_image_id,
        )
        if claimed is None:
            emit(
                f"Run {run_id} already has a verification. One verification per patch "
                f"in this milestone; exiting without starting any container."
            )
            return EXIT_NOT_CLAIMED
        verification_id = claimed.id

    emit(f"Claimed verification for run {run_id}")
    emit(f"Profile: {config.verify_profile} | image {config.verify_image} ({resolved_image_id[:19]})")
    emit(f"Commit: {commit_sha}")
    emit("Repository code runs only inside a disposable container with no network.")

    def record(kind: str, summary: str, detail: str | None) -> None:
        emit(f"  · {summary}")

    # --- 4. Snapshot, containers, comparison — no session held open ----------
    try:
        result = verification.run_verification(
            repository_url=repository_url,
            commit_sha=commit_sha,
            diff=diff,
            config=config,
            image_id=resolved_image_id,
            record=record,
            snapshot_fetcher=snapshot_fetcher,
            test_runner=test_runner,
        )
    except (snapshot_module.SnapshotError, runner.RunnerError) as error:
        with _session(session_factory) as db:
            repository.save_verification_failure(
                db,
                verification_id=verification_id,
                error_kind=getattr(error, "kind", "verification_error"),
                error_message=str(error),
                max_error_chars=config.verify_max_error_chars,
            )
        emit(f"Verification failed ({getattr(error, 'kind', 'error')}): {error}")
        return EXIT_VERIFICATION_FAILED

    # --- 5. Persist the outcome ---------------------------------------------
    with _session(session_factory) as db:
        repository.save_verification_result(
            db,
            verification_id=verification_id,
            outcome=result.outcome,
            detail=result.detail,
            patch_applied=result.patch_applied,
            patch_apply_message=result.apply_message,
            patch_touched_tests=result.patch_touched_tests,
            files_changed=result.files_changed,
            runner_args=result.runner_args,
            baseline_summary=result.baseline.to_json() if result.baseline else None,
            patched_summary=result.patched.to_json() if result.patched else None,
            comparison=result.comparison.to_json() if result.comparison else None,
            baseline_log=result.baseline_log,
            patched_log=result.patched_log,
            supplemental_summary=(
                result.supplemental.to_json() if result.supplemental else None
            ),
            supplemental_log=result.supplemental_log,
            notes=result.notes,
            max_log_chars=config.verify_max_log_bytes,
            max_error_chars=config.verify_max_error_chars,
        )

    emit("")
    emit(f"Outcome: {result.outcome}")
    emit(f"  {result.detail}")
    if result.baseline is not None:
        emit(
            f"  baseline: {result.baseline.kind} — {len(result.baseline.failing)} failing "
            f"of {len(result.baseline.collected)} collected"
        )
    if result.patched is not None:
        emit(
            f"  patched : {result.patched.kind} — {len(result.patched.failing)} failing "
            f"of {len(result.patched.collected)} collected"
        )
    if result.supplemental is not None:
        emit(f"  supplemental (the patch's own tests): {result.supplemental.kind}")
    for note in result.notes:
        emit(f"  note: {note}")
    for problem in result.cleanup_errors:
        emit(f"  cleanup: {problem}")
    emit("")
    emit(
        "This covers only the tests that ran, on this commit, in this runner "
        "profile. It is not proof the patch is correct."
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description=(
            "BranchForge inspection worker. Reads a public GitHub repository "
            "through the API and stores a report. Never clones or executes "
            "repository code."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect the repository for one pending run."
    )
    inspect_parser.add_argument(
        "--run-id", required=True, help="UUID of the run to inspect."
    )

    propose_parser = subparsers.add_parser(
        "propose",
        help="Propose a patch for one inspected run using the configured model.",
    )
    propose_parser.add_argument(
        "--run-id", required=True, help="UUID of an inspected ('ready') run."
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="Apply the proposed patch and run the repository's tests in Docker.",
    )
    verify_parser.add_argument(
        "--run-id", required=True, help="UUID of a run with a proposed patch."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        return inspect_run(args.run_id)
    if args.command == "propose":
        return propose_patch(args.run_id)
    if args.command == "verify":
        return verify_patch(args.run_id)
    return EXIT_OK  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":
    raise SystemExit(main())
