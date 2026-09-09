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

from app import github_client, repository
from app.config import Settings, settings
from app.database import SessionLocal
from app.github_client import ClientLimits, GitHubClient, GitHubError
from app.inspection import inspect_repository
from app.models import RUN_STATUS_PENDING
from app.time_utils import utcnow

EXIT_OK = 0
EXIT_INSPECTION_FAILED = 1
EXIT_RUN_NOT_FOUND = 2
EXIT_NOT_CLAIMED = 3

SessionFactory = Callable[[], Session] | sessionmaker[Session]
ClientFactory = Callable[[Settings], GitHubClient]


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        return inspect_run(args.run_id)
    return EXIT_OK  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":
    raise SystemExit(main())
