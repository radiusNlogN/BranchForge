"""Real containers. Skipped when Docker is unavailable — never faked.

Everything above this file mocks the container. This file does not: it builds
real workspaces, runs the real image, and asserts on what actually happened. It
is the only evidence that the isolation flags and the reporting plugin work.

Requires the runner image:
    docker build -t branchforge-runner-python:1 backend/runner/python-pytest
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from app import runner, verification
from app.config import Settings
from app.runner import RunnerLimits
from app.snapshot import SnapshotResult
from app.verification import (
    OUTCOME_COLLECTION_MISMATCH,
    OUTCOME_FIX_DEMONSTRATED,
    OUTCOME_PATCH_DID_NOT_APPLY,
    OUTCOME_PATCHED_COLLECTION_ERROR,
    OUTCOME_REGRESSIONS,
    OUTCOME_STILL_FAILING,
    OUTCOME_TESTS_ONLY_PATCH,
    OUTCOME_TIMEOUT,
)
from tests import fixture_support as fx

pytestmark = pytest.mark.docker

SHA = "b" * 40
REPO_URL = "https://github.com/example/sample"

_DOCKER_UP = runner.docker_available()


def _image_present() -> bool:
    if not _DOCKER_UP:
        return False
    try:
        runner.resolve_image_id(Settings().verify_image, limits=RunnerLimits())
        return True
    except runner.DockerUnavailable:
        return False


requires_docker = pytest.mark.skipif(
    not _image_present(),
    reason=(
        "Docker daemon or runner image unavailable. Build it with: "
        "docker build -t branchforge-runner-python:1 backend/runner/python-pytest"
    ),
)


@pytest.fixture(scope="module")
def image_id():
    return runner.resolve_image_id(Settings().verify_image, limits=RunnerLimits())


def local_snapshot(*, owner, repo, commit_sha, destination):
    fx.copy_sample_repo(destination)
    return SnapshotResult(
        commit_sha=commit_sha, root=destination, file_count=4,
        decompressed_bytes=1000, download_bytes=500,
    )


def verify(diff, tmp_path, image_id, **overrides):
    settings_kwargs = {"database_url": "sqlite://"}
    settings_kwargs.update(overrides)
    config = Settings(**settings_kwargs)
    root = str(tmp_path / "work")
    os.makedirs(root, exist_ok=True)
    events = []
    result = verification.run_verification(
        repository_url=REPO_URL,
        commit_sha=SHA,
        diff=diff,
        config=config,
        image_id=image_id,
        record=lambda kind, summary, detail: events.append((kind, summary)),
        snapshot_fetcher=local_snapshot,
        test_runner=None,  # the real Docker runner
        workspace_root=root,
    )
    return result, events, root


# --- The runner itself ------------------------------------------------------


@requires_docker
def test_the_baseline_run_reproduces_the_known_bug(tmp_path, image_id):
    """Ground truth: the fixture really does fail before any patch."""
    workspace = str(tmp_path / "ws")
    fx.copy_sample_repo(workspace)
    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=120),
    )
    assert result.exit_code == 1
    summary = verification.classify_run(result)
    assert summary.kind == verification.RUN_TESTS_FAILED
    assert summary.failing == {"tests/test_calc.py::test_add"}
    assert "tests/test_calc.py::test_identity" in summary.passing
    assert len(summary.collected) == 4


@requires_docker
def test_the_container_really_has_no_network(tmp_path, image_id):
    workspace = str(tmp_path / "ws")
    os.makedirs(os.path.join(workspace, "tests"))
    with open(os.path.join(workspace, "tests", "test_net.py"), "w") as handle:
        handle.write(
            "import socket\n"
            "def test_no_network():\n"
            "    try:\n"
            "        socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "    except OSError:\n"
            "        return\n"
            "    raise AssertionError('network was reachable')\n"
        )
    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=60),
    )
    assert result.exit_code == 0, result.log


@requires_docker
def test_the_container_is_not_root_and_the_source_is_read_only(tmp_path, image_id):
    workspace = str(tmp_path / "ws")
    os.makedirs(os.path.join(workspace, "tests"))
    with open(os.path.join(workspace, "tests", "test_env.py"), "w") as handle:
        handle.write(
            "import os\n"
            "def test_not_root():\n"
            "    assert os.getuid() == 65534\n"
            "def test_workspace_is_read_only():\n"
            "    try:\n"
            "        open('/workspace/written.txt', 'w').write('x')\n"
            "    except OSError:\n"
            "        return\n"
            "    raise AssertionError('workspace was writable')\n"
            "def test_tmp_is_writable():\n"
            "    open('/tmp/ok.txt', 'w').write('x')\n"
        )
    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=60),
    )
    assert result.exit_code == 0, result.log


@requires_docker
def test_a_hanging_test_times_out_and_leaves_no_container_behind(tmp_path, image_id):
    """Timeout cleanup, driven by a hanging *original* test.

    Deliberately independent of supplemental-test behaviour: this is the runner's
    own wall-clock limit and its container teardown.
    """
    workspace = str(tmp_path / "ws")
    os.makedirs(os.path.join(workspace, "tests"))
    with open(os.path.join(workspace, "tests", "test_hang.py"), "w") as handle:
        handle.write("import time\ndef test_hang():\n    time.sleep(600)\n")

    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=8),
    )

    assert result.timed_out is True
    assert result.report is None
    assert "wall-clock limit" in (result.report_error or "")
    assert result.cleanup_errors == [], result.cleanup_errors
    assert verification.classify_run(result).kind == verification.RUN_TIMEOUT

    # Nothing left running or lingering.
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={result.container_name}", "-q"],
        capture_output=True, text=True, timeout=30,
    )
    assert listed.stdout.strip() == "", "the container was not removed"


@requires_docker
def test_excessive_output_is_capped_without_hanging(tmp_path, image_id):
    """The reader keeps draining past the cap, so a full pipe cannot deadlock."""
    workspace = str(tmp_path / "ws")
    os.makedirs(os.path.join(workspace, "tests"))
    # The test fails on purpose: pytest captures stdout from passing tests, so a
    # noisy *passing* test would never reach the container's stdout at all.
    with open(os.path.join(workspace, "tests", "test_loud.py"), "w") as handle:
        handle.write(
            "def test_loud():\n"
            "    for _ in range(200):\n"
            "        print('y' * 50000)\n"
            "    assert False\n"
        )
    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=120, max_log_bytes=50_000),
    )

    assert result.timed_out is False, "a capped reader must not stall the container"
    assert result.log_truncated is True
    assert len(result.log.encode()) <= 50_000
    # The run still completed and its structured report survived the flood.
    assert result.report is not None
    assert verification.classify_run(result).kind == verification.RUN_TESTS_FAILED


# --- End-to-end scenarios ---------------------------------------------------


@requires_docker
def test_a_correct_patch_makes_the_failing_test_pass(tmp_path, image_id):
    result, _, _ = verify(fx.correct_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_FIX_DEMONSTRATED, result.detail
    assert result.comparison.fixed == ["tests/test_calc.py::test_add"]
    assert result.comparison.regressions == []
    assert result.baseline.kind == verification.RUN_TESTS_FAILED
    assert result.patched.kind == verification.RUN_OK


@requires_docker
def test_a_patch_that_does_not_apply_is_reported_as_such(tmp_path, image_id):
    result, _, _ = verify(fx.non_applying_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_PATCH_DID_NOT_APPLY
    assert result.patch_applied is False
    assert result.baseline.kind == verification.RUN_TESTS_FAILED


@requires_docker
def test_an_incorrect_patch_that_breaks_other_tests_reports_regressions(tmp_path, image_id):
    result, _, _ = verify(fx.incorrect_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_REGRESSIONS, result.detail
    assert "tests/test_calc.py::test_identity" in result.comparison.regressions


@requires_docker
def test_an_ineffective_patch_is_still_failing(tmp_path, image_id):
    result, _, _ = verify(fx.ineffective_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_STILL_FAILING, result.detail
    assert result.comparison.fixed == []


@requires_docker
def test_deleting_the_failing_test_does_not_produce_a_fix(tmp_path, image_id):
    result, _, root = verify(fx.deletes_failing_test_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_TESTS_ONLY_PATCH
    assert os.path.isfile(os.path.join(root, "comparison", "tests", "test_calc.py"))


@requires_docker
def test_skipping_the_failing_test_from_source_does_not_produce_a_fix(tmp_path, image_id):
    """The test file is untouched; the source flips a flag that makes it skip.

    Restoring the original tests cannot catch this, which is exactly why a fix
    requires an explicit `passed`.
    """
    result, _, _ = verify(fx.skips_failing_test_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_STILL_FAILING, result.detail
    assert result.comparison.fixed == []
    assert result.comparison.no_longer_exercised == ["tests/test_calc.py::test_add"]


@requires_docker
def test_uncollecting_tests_is_reported_as_a_collection_mismatch(tmp_path, image_id):
    result, _, _ = verify(fx.uncollects_tests_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_COLLECTION_MISMATCH, result.detail
    assert "tests/test_calc.py::test_cases[beta]" in result.comparison.missing_from_patched


@requires_docker
def test_a_patch_that_breaks_import_is_not_an_improvement(tmp_path, image_id):
    result, _, _ = verify(fx.breaks_import_patch(), tmp_path, image_id)
    assert result.outcome == OUTCOME_PATCHED_COLLECTION_ERROR, result.detail
    assert result.baseline.kind == verification.RUN_TESTS_FAILED
    assert result.patched.kind == verification.RUN_COLLECTION_ERROR


@requires_docker
def test_a_patch_that_hangs_the_application_times_out(tmp_path, image_id):
    """A hanging patched application function, with a short runner timeout."""
    result, _, _ = verify(
        fx.hanging_source_patch(), tmp_path, image_id, verify_test_timeout_seconds=10.0
    )
    assert result.outcome == OUTCOME_TIMEOUT, result.detail
    assert result.patched is not None and result.patched.timed_out is True
    assert result.cleanup_errors == []


@requires_docker
def test_the_report_plugin_records_every_outcome_kind(tmp_path, image_id):
    """Skips, xfails, and errors are distinguished, not lumped into pass/fail."""
    workspace = str(tmp_path / "ws")
    os.makedirs(os.path.join(workspace, "tests"))
    with open(os.path.join(workspace, "tests", "test_kinds.py"), "w") as handle:
        handle.write(
            "import pytest\n"
            "def test_pass():\n    assert True\n"
            "def test_fail():\n    assert False\n"
            "@pytest.mark.skip(reason='x')\n"
            "def test_skip():\n    assert False\n"
            "@pytest.mark.xfail\n"
            "def test_xfail():\n    assert False\n"
            "@pytest.fixture\n"
            "def broken():\n    raise RuntimeError('setup boom')\n"
            "def test_error(broken):\n    assert True\n"
        )
    result = runner.run_tests(
        workspace=workspace,
        results_dir=str(tmp_path / "res"),
        image_id=image_id,
        limits=RunnerLimits(timeout_seconds=60),
    )
    summary = verification.classify_run(result)
    outcomes = {node.split("::")[-1]: kind for node, kind in summary.outcomes.items()}
    assert outcomes["test_pass"] == "passed"
    assert outcomes["test_fail"] == "failed"
    assert outcomes["test_skip"] == "skipped"
    assert outcomes["test_xfail"] == "xfailed"
    # A fixture that explodes is an error, not a test failure.
    assert outcomes["test_error"] == "error"
