"""A test-only inspect child for dispatcher tests. Not collected (no `test_` prefix).

    python -m tests.dispatcher_inspect_child --run-id R --scenario S

Runs the REAL `worker.inspect_run` with GitHub replaced at the usual transport
seam, so the claim, the persisted report, and the run's status transition all
execute for real. A mocked transport cannot cross a process boundary, so it is
rebuilt here from the scenario file — the same trick `tests.orchestration_child`
uses for the model.

scenario["inspect"] may set:

    behaviour: ok | fail | hang     (default "ok")

"hang" exists so a test can cancel a job *during* inspection. The real inspector
installs no SIGTERM handler, and neither does this, so the child dies mid-flight
exactly as the real one would — which is the case the dispatcher has to reconcile.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx2

from app import worker
from tests.conftest import FakeGitHub


def _wait_forever() -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(0.05)
    raise SystemExit(99)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)
    workdir = scenario["workdir"]
    spec = scenario.get("inspect", {})
    behaviour = spec.get("behaviour", "ok")

    with open(os.path.join(workdir, "inspect-pid"), "w") as handle:
        handle.write(str(os.getpid()))
    open(os.path.join(workdir, "inspect-started"), "w").close()

    if behaviour == "hang":
        # Claim the run FIRST, then hang. A real inspector claims (pending ->
        # inspecting) and only then does its network work, so killing it leaves
        # the run stranded in `inspecting` — which is exactly the state the
        # dispatcher has to reconcile. Hanging before the claim would leave the
        # run `pending` and quietly test nothing.
        from app import repository
        from app.database import SessionLocal

        with SessionLocal() as db:
            repository.claim_run_for_inspection(db, args.run_id)
        open(os.path.join(workdir, "inspect-claimed"), "w").close()
        _wait_forever()

    if behaviour == "fail":
        # A repository that cannot be read: the real client raises GitHubError and
        # the real worker records an inspection failure and fails the run.
        github = FakeGitHub(
            responses={"/repos/o/r": httpx2.Response(404, json={"message": "Not Found"})}
        )
    else:
        github = FakeGitHub()

    return worker.inspect_run(
        args.run_id,
        client_factory=github.client_factory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
