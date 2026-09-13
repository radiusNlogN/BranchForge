"""A test-only dispatcher process, so real signals can be sent to it.

    python -m tests.dispatcher_driver --scenario S --database-url URL
        [--poll SECONDS] [--grace SECONDS] [--worker-grace SECONDS]
        [--cancel-poll SECONDS] [--docker-binary PATH]

Runs the REAL `run_dispatcher` in its own process. Only three things are
injected, all at the seams the production code already exposes:

* the model factory, so no provider is contacted;
* the image resolver, so no Docker daemon is needed for preflight;
* the two child commands, which point at the test-only harnesses.

Everything the tests are actually about — the OS lock, the claim, staging,
cancellation, reconciliation from the database, orphan reaping, and the container
sweep — is the production code path, unchanged.

There is deliberately **one** such entry point rather than one per test tier: the
browser tests drive this same driver. Adding a second way to run a mocked
dispatcher would mean the thing under test differed between tiers.

Not collected (no `test_` prefix).
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dispatcher import run_dispatcher
from tests.conftest import ScriptedModelClient, agent_settings


def inspect_command_for(scenario_path: str):
    def command(run_id: str) -> list[str]:
        return [
            sys.executable, "-m", "tests.dispatcher_inspect_child",
            "--run-id", run_id,
            "--scenario", scenario_path,
        ]

    return command


def orchestrate_command_for(
    scenario_path: str, database_url: str, *, concurrency: int, grace: float, docker_binary: str
):
    def command(run_id: str) -> list[str]:
        return [
            sys.executable, "-m", "tests.orchestration_driver",
            "--run-id", run_id,
            "--scenario", scenario_path,
            "--database-url", database_url,
            "--concurrency", str(concurrency),
            "--grace", str(grace),
            "--docker-binary", docker_binary,
        ]

    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--cancel-poll", type=float, default=0.1)
    parser.add_argument("--grace", type=float, default=10.0)
    parser.add_argument("--worker-grace", type=float, default=3.0)
    parser.add_argument("--orchestrator-grace", type=float, default=3.0)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--docker-binary", default="docker")
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)
    image_id = scenario.get("image_id", "sha256:testimage")

    config = agent_settings(
        database_url=args.database_url,
        dispatcher_poll_seconds=args.poll,
        dispatcher_cancel_poll_seconds=args.cancel_poll,
        dispatcher_child_grace_seconds=args.grace,
        dispatcher_worker_grace_seconds=args.worker_grace,
        dispatcher_drain_timeout_seconds=5.0,
        orchestrator_max_concurrency=args.concurrency,
        orchestrator_child_grace_seconds=args.orchestrator_grace,
        verify_docker_binary=args.docker_binary,
    )
    engine = create_engine(
        args.database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    return run_dispatcher(
        session_factory=factory,
        config=config,
        model_factory=lambda _config: ScriptedModelClient([]),
        image_resolver=lambda: image_id,
        inspect_command=inspect_command_for(args.scenario),
        orchestrate_command=orchestrate_command_for(
            args.scenario,
            args.database_url,
            concurrency=args.concurrency,
            grace=args.orchestrator_grace,
            docker_binary=args.docker_binary,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
