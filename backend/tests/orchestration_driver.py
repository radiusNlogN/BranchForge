"""A test-only orchestrator process, so real signals can be sent to it.

    python -m tests.orchestration_driver --run-id R --scenario S --database-url URL
        [--concurrency N] [--grace SECONDS] [--docker-binary PATH] [--test-timeout S]

Runs the real `orchestrate_run` in its own process with the test child command.
Not collected (no `test_` prefix).
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.orchestrator import ChildSpec, orchestrate_run
from tests.conftest import ScriptedModelClient, agent_settings


def child_command_for(scenario_path: str):
    def command(spec: ChildSpec) -> list[str]:
        return [
            sys.executable, "-m", "tests.orchestration_child",
            "--attempt-id", spec.attempt_id,
            "--orchestration-id", spec.orchestration_id,
            "--scenario", scenario_path,
        ]

    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--grace", type=float, default=5.0)
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--test-timeout", type=float, default=300.0)
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)

    config = agent_settings(
        database_url=args.database_url,
        orchestrator_max_concurrency=args.concurrency,
        orchestrator_child_grace_seconds=args.grace,
        verify_docker_binary=args.docker_binary,
        verify_test_timeout_seconds=args.test_timeout,
    )
    engine = create_engine(
        args.database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return orchestrate_run(
        args.run_id,
        session_factory=factory,
        model_factory=lambda config: ScriptedModelClient([]),
        config=config,
        image_id=scenario.get("image_id"),
        child_command=child_command_for(args.scenario),
    )


if __name__ == "__main__":
    raise SystemExit(main())
