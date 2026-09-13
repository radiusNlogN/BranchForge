"""A test-only orchestrator child. Not collected (no `test_` prefix).

    python -m tests.orchestration_child --attempt-id A --orchestration-id O --scenario S

A scripted model cannot cross a process boundary, so the child rebuilds one from a
JSON scenario and then runs the REAL child code path —
`worker.run_attempt_pipeline` — with the model, GitHub, snapshot, and (unless the
scenario says "docker") container runner replaced at the usual mocking seams.

Per-attempt behaviours (scenario["attempts"][str(index)]):

  behaviour: submit | fail | hang | ignore_term | crash_in_proposal
  patch:     correct | incorrect | ineffective | noisy | hanging   (for submit)
  runner:    fixing | failing | regressing | hang | crash | docker
  after_proposal_exit: <int>   exit this way after the proposal is saved and
                                 before the verification is claimed

Evidence is written under scenario["workdir"]: pid-<i>, prompt-<i>.txt,
workspaces-<i>.txt, arrived-<i>, and an flock-guarded active/max counter.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import signal
import sys
import time
from contextlib import contextmanager

from app import repository, worker
from app.database import SessionLocal
from app.model_client import ModelRequestFailed
from app.runner import RunResult
from app.snapshot import SnapshotResult
from tests import fixture_support as fx
from tests.conftest import FakeGitHub, ScriptedModelClient, make_turn, read_call, submit_call

ADD = "tests/test_calc.py::test_add"
IDENTITY = "tests/test_calc.py::test_identity"
ALPHA = "tests/test_calc.py::test_cases[alpha]"
BETA = "tests/test_calc.py::test_cases[beta]"
ALL_TESTS = [ADD, IDENTITY, ALPHA, BETA]

PATCHES = {
    "correct": fx.correct_patch,
    "incorrect": fx.incorrect_patch,
    "ineffective": fx.ineffective_patch,
    "noisy": fx.noisy_patch,
    "hanging": fx.hanging_source_patch,
}


def make_report(outcomes: dict[str, str]) -> dict:
    return {
        "schema": "branchforge.report.v1",
        "exitstatus": 1 if any(o in ("failed", "error") for o in outcomes.values()) else 0,
        "collected": list(outcomes),
        "collect_errors": [],
        "tests": {n: {"outcome": o, "phases": {}} for n, o in outcomes.items()},
        "counts": {},
        "truncated": False,
    }


BASELINE = make_report({ADD: "failed", IDENTITY: "passed", ALPHA: "passed", BETA: "passed"})
PATCHED = {
    "fixing": make_report({n: "passed" for n in ALL_TESTS}),
    "failing": BASELINE,
    "regressing": make_report({ADD: "failed", IDENTITY: "failed", ALPHA: "passed", BETA: "passed"}),
}


def _result(report: dict, image_id: str) -> RunResult:
    return RunResult(
        exit_code=report["exitstatus"], log="scripted container log", log_truncated=False,
        duration_seconds=0.1, report=report, argv=["docker", "run", "--network=none", image_id],
    )


def _wait_forever() -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(0.05)
    raise SystemExit(99)


@contextmanager
def _locked(path: str):
    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_int(path: str) -> int:
    try:
        with open(path) as handle:
            return int(handle.read().strip() or 0)
    except FileNotFoundError:
        return 0


def adjust_active(workdir: str, delta: int) -> None:
    with _locked(os.path.join(workdir, "active.lock")):
        current = _read_int(os.path.join(workdir, "active")) + delta
        with open(os.path.join(workdir, "active"), "w") as handle:
            handle.write(str(current))
        if current > _read_int(os.path.join(workdir, "max_active")):
            with open(os.path.join(workdir, "max_active"), "w") as handle:
                handle.write(str(current))


class ChildModel(ScriptedModelClient):
    """Scripted turns plus the synchronization behaviours tests need."""

    def __init__(self, turns, *, index: int, behaviour: str, scenario: dict) -> None:
        super().__init__(turns)
        self.index = index
        self.behaviour = behaviour
        self.scenario = scenario
        self.workdir = scenario["workdir"]

    def create_message(self, *, system, messages, tools, max_tokens):
        if self.calls == 0:
            with open(os.path.join(self.workdir, f"prompt-{self.index}.txt"), "w") as handle:
                handle.write(messages[0]["content"])
            open(os.path.join(self.workdir, f"arrived-{self.index}"), "w").close()
            barrier = self.scenario.get("barrier")
            if barrier:
                # A real barrier: it can only release if `barrier` children are
                # simultaneously inside their proposal. No timing threshold.
                deadline = time.monotonic() + self.scenario.get("barrier_timeout", 60)
                while True:
                    arrived = [n for n in os.listdir(self.workdir) if n.startswith("arrived-")]
                    if len(arrived) >= barrier:
                        break
                    if time.monotonic() > deadline:
                        raise ModelRequestFailed("barrier timed out: attempts did not overlap")
                    time.sleep(0.02)
        if self.behaviour == "crash_in_proposal":
            os._exit(9)
        if self.behaviour == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _wait_forever()
        if self.behaviour == "hang":
            _wait_forever()
        return super().create_message(system=system, messages=messages, tools=tools, max_tokens=max_tokens)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--orchestration-id", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    worker.install_child_signal_handlers()

    with open(args.scenario) as handle:
        scenario = json.load(handle)
    workdir = scenario["workdir"]

    with SessionLocal() as db:
        attempt = repository.get_patch_attempt(db, args.attempt_id)
        index = attempt.attempt_index
    spec = scenario["attempts"][str(index)]
    behaviour = spec.get("behaviour", "submit")

    with open(os.path.join(workdir, f"pid-{index}"), "w") as handle:
        handle.write(str(os.getpid()))
    adjust_active(workdir, +1)
    atexit.register(adjust_active, workdir, -1)

    own_file = f"src/file_{index}.py"
    if behaviour == "fail":
        turns = [make_turn(tool_calls=[read_call(own_file)]), make_turn(text="No safe fix found.")]
    else:
        diff = PATCHES[spec.get("patch", "correct")]()
        turns = [
            make_turn(tool_calls=[read_call(own_file)]),
            make_turn(tool_calls=[submit_call(diff=diff, summary=f"Attempt {index} patch.")]),
        ]
    model = ChildModel(turns, index=index, behaviour=behaviour, scenario=scenario)
    github = FakeGitHub(files={own_file: f"# file for attempt {index}\n"})

    def snapshot_fetcher(*, owner, repo, commit_sha, destination):
        fx.copy_sample_repo(destination)
        return SnapshotResult(
            commit_sha=commit_sha, root=destination, file_count=4,
            decompressed_bytes=1000, download_bytes=500,
        )

    runner_mode = spec.get("runner", "fixing")
    image_id = scenario.get("image_id", "sha256:testimage")

    def fake_runner(*, workspace, results_dir, image_id=image_id):
        with open(os.path.join(workdir, f"workspaces-{index}.txt"), "a") as handle:
            handle.write(workspace + "\n")
        if runner_mode == "crash":
            os._exit(0)
        if runner_mode == "hang":
            _wait_forever()
        phase = os.path.basename(workspace)
        if phase == "baseline":
            return _result(BASELINE, image_id)
        return _result(PATCHED[runner_mode], image_id)

    after_proposal = None
    if "after_proposal_exit" in spec:
        code = int(spec["after_proposal_exit"])

        def after_proposal() -> None:
            sys.stdout.flush()
            os._exit(code)

    return worker.run_attempt_pipeline(
        args.attempt_id,
        args.orchestration_id,
        model_factory=lambda config: model,
        client_factory=github.client_factory,
        snapshot_fetcher=snapshot_fetcher,
        test_runner=None if runner_mode == "docker" else fake_runner,
        after_proposal=after_proposal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
