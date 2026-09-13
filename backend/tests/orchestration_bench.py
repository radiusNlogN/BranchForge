"""A LOCAL SCRIPTED BENCHMARK: the same 3-attempt workload at concurrency 1 vs 3.

    uv run python -m tests.orchestration_bench [--docker]

Proposals are scripted (no model calls, no network), so this measures process
management and verification only. Without --docker the container runner is also
scripted; with --docker every attempt runs its real baseline and patched
containers. The numbers describe this machine and this toy fixture repository.
They say nothing about speedups with a real model, whose latency, rate limits,
and cost dominate a real orchestration.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import time
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import orchestrator, repository, runner, worker
from app.config import Settings
from app.runner import RunnerLimits
from tests.conftest import (
    INSPECTABLE_PAYLOAD,
    FakeGitHub,
    ScriptedModelClient,
    agent_settings,
    alembic_config,
)
from tests.orchestration_driver import child_command_for


def one(concurrency: int, docker: bool, image_id: str) -> float:
    root = Path(tempfile.mkdtemp(prefix="bf-bench-"))
    url = f"sqlite:///{root / 'bench.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with factory() as db:
        run = repository.create_run(
            db, repository_url=INSPECTABLE_PAYLOAD["repository_url"],
            issue_description=INSPECTABLE_PAYLOAD["issue_description"], max_parallel_attempts=3,
        )
        run_id = run.id
    worker.inspect_run(
        run_id, session_factory=factory, client_factory=FakeGitHub().client_factory,
        config=Settings(database_url=url, github_api_base_url="https://api.github.test"),
        out=io.StringIO(),
    )

    workdir = root / "work"
    workdir.mkdir()
    mode = "docker" if docker else "fixing"
    scenario = root / "scenario.json"
    scenario.write_text(json.dumps({
        "workdir": str(workdir), "image_id": image_id,
        "attempts": {
            "1": {"patch": "correct", "runner": mode},
            "2": {"patch": "ineffective", "runner": mode if docker else "failing"},
            "3": {"patch": "incorrect", "runner": mode if docker else "regressing"},
        },
    }))

    fake_docker = root / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n")
    fake_docker.chmod(0o755)

    started = time.monotonic()
    code = orchestrator.orchestrate_run(
        run_id,
        session_factory=factory,
        model_factory=lambda c: ScriptedModelClient([]),
        config=agent_settings(
            database_url=url, orchestrator_max_concurrency=concurrency,
            verify_docker_binary="docker" if docker else str(fake_docker),
        ),
        image_id=image_id,
        child_command=child_command_for(str(scenario)),
        out=io.StringIO(),
    )
    elapsed = time.monotonic() - started
    if code != worker.EXIT_OK:
        raise SystemExit(f"orchestration exited {code}")
    engine.dispose()
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    image_id = (
        runner.resolve_image_id(Settings().verify_image, limits=RunnerLimits())
        if args.docker else "sha256:scripted"
    )
    print(f"LOCAL SCRIPTED BENCHMARK — 3 attempts, verification {'real Docker' if args.docker else 'scripted'}")
    for concurrency in (1, 3):
        times = [one(concurrency, args.docker, image_id) for _ in range(args.repeat)]
        print(
            f"  concurrency {concurrency}: "
            + ", ".join(f"{t:.1f}s" for t in times)
            + f"  (median {sorted(times)[len(times) // 2]:.1f}s)"
        )
    print("Not a claim about real-model speedups.")


if __name__ == "__main__":
    main()
