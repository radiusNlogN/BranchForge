"""Orchestration and the verdict.

The comparison logic is where a verifier earns or loses its credibility, so most
of these tests are about refusing to claim a fix: a test that vanishes, gets
skipped, stops being collected, or never ran at all must not read as success.

Docker is not involved. The container is replaced by a scripted runner; snapshot
acquisition, `git apply`, and test-environment restoration are all real.
"""

from __future__ import annotations

import os

import pytest

from app import verification
from app.config import Settings
from app.runner import RunResult
from app.snapshot import SnapshotResult
from app.verification import (
    OUTCOME_COLLECTION_MISMATCH,
    OUTCOME_FIX_DEMONSTRATED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_NO_BUG_DEMONSTRATED,
    OUTCOME_PATCH_DID_NOT_APPLY,
    OUTCOME_PATCHED_COLLECTION_ERROR,
    OUTCOME_PARTIAL_FIX,
    OUTCOME_REGRESSIONS,
    OUTCOME_STILL_FAILING,
    OUTCOME_TESTS_ONLY_PATCH,
    OUTCOME_TIMEOUT,
    OUTCOME_UNSUPPORTED_LAYOUT,
    RunSummary,
    classify_run,
    compare_runs,
)
from tests import fixture_support as fx

ADD = "tests/test_calc.py::test_add"
IDENTITY = "tests/test_calc.py::test_identity"
ALPHA = "tests/test_calc.py::test_cases[alpha]"
BETA = "tests/test_calc.py::test_cases[beta]"
ALL_TESTS = [ADD, IDENTITY, ALPHA, BETA]

SHA = "a" * 40
REPO_URL = "https://github.com/example/sample"


def make_report(outcomes, *, collected=None, exitstatus=None, collect_errors=None, truncated=False):
    """Build a report payload shaped exactly like the runner plugin's output."""
    collected = list(outcomes) if collected is None else collected
    if exitstatus is None:
        exitstatus = 1 if any(o in ("failed", "error") for o in outcomes.values()) else 0
    counts = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    return {
        "schema": "branchforge.report.v1",
        "exitstatus": exitstatus,
        "collected": collected,
        "collect_errors": collect_errors or [],
        "tests": {node: {"outcome": outcome, "phases": {}} for node, outcome in outcomes.items()},
        "counts": counts,
        "truncated": truncated,
    }


def make_result(report=None, *, exit_code=1, log="", timed_out=False, report_error=None):
    return RunResult(
        exit_code=exit_code,
        log=log,
        log_truncated=False,
        duration_seconds=0.1,
        timed_out=timed_out,
        report=report,
        report_error=report_error,
        argv=["docker", "run", "..."],
    )


BASELINE_REPORT = make_report(
    {ADD: "failed", IDENTITY: "passed", ALPHA: "passed", BETA: "passed"}
)


class ScriptedRunner:
    """Returns a canned RunResult per phase, keyed by workspace directory name."""

    def __init__(self, by_phase):
        self.by_phase = by_phase
        self.workspaces = []

    def __call__(self, *, workspace, results_dir, image_id):
        phase = os.path.basename(workspace)
        self.workspaces.append(phase)
        result = self.by_phase.get(phase)
        if result is None:
            raise AssertionError(f"unexpected run against {phase!r}")
        return result


def local_snapshot(*, owner, repo, commit_sha, destination):
    """Stands in for codeload: copies the fixture repository."""
    fx.copy_sample_repo(destination)
    return SnapshotResult(
        commit_sha=commit_sha, root=destination, file_count=4,
        decompressed_bytes=1000, download_bytes=500,
    )


def verify(diff, runner_script, tmp_path, *, config=None, snapshot_fetcher=None):
    events = []
    root = str(tmp_path / "work")
    os.makedirs(root, exist_ok=True)
    result = verification.run_verification(
        repository_url=REPO_URL,
        commit_sha=SHA,
        diff=diff,
        config=config or Settings(database_url="sqlite://"),
        image_id="sha256:test",
        record=lambda kind, summary, detail: events.append((kind, summary)),
        snapshot_fetcher=snapshot_fetcher or local_snapshot,
        test_runner=runner_script,
        workspace_root=root,  # kept so the workspaces can be inspected
    )
    return result, events, root


# --- The verdict, unit-tested directly --------------------------------------


def summary(outcomes, *, kind=None, collected=None):
    report = make_report(outcomes, collected=collected)
    return classify_run(make_result(report, exit_code=report["exitstatus"]))


def test_a_previously_failing_test_that_passes_is_a_fix():
    baseline = summary({ADD: "failed", IDENTITY: "passed"})
    patched = summary({ADD: "passed", IDENTITY: "passed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_FIX_DEMONSTRATED
    assert comparison.fixed == [ADD]


@pytest.mark.parametrize("vanished_as", ["skipped", "xfailed", "xpassed"])
def test_a_failing_test_that_stops_running_is_not_a_fix(vanished_as):
    """Disappearing from the failing set is not the same as passing."""
    baseline = summary({ADD: "failed", IDENTITY: "passed"})
    patched = summary({ADD: vanished_as, IDENTITY: "passed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_STILL_FAILING
    assert comparison.fixed == []
    assert comparison.no_longer_exercised == [ADD]


def test_a_partial_fix_is_named_as_such():
    baseline = summary({ADD: "failed", IDENTITY: "failed"})
    patched = summary({ADD: "passed", IDENTITY: "failed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_PARTIAL_FIX
    assert comparison.fixed == [ADD] and comparison.still_failing == [IDENTITY]


def test_a_setup_error_counts_as_failing_not_passing():
    baseline = summary({ADD: "error", IDENTITY: "passed"})
    patched = summary({ADD: "passed", IDENTITY: "passed"})
    assert compare_runs(baseline, patched).outcome == OUTCOME_FIX_DEMONSTRATED


def test_regressions_outrank_a_fix():
    baseline = summary({ADD: "failed", IDENTITY: "passed"})
    patched = summary({ADD: "passed", IDENTITY: "failed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_REGRESSIONS
    assert comparison.regressions == [IDENTITY]


def test_a_previously_passing_test_that_stops_running_is_inconclusive():
    """The yardstick was weakened, so the comparison cannot be trusted."""
    baseline = summary({ADD: "failed", IDENTITY: "passed"})
    patched = summary({ADD: "passed", IDENTITY: "skipped"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_INCONCLUSIVE
    assert comparison.weakened == [IDENTITY]


def test_a_green_baseline_demonstrates_no_bug():
    baseline = summary({ADD: "passed", IDENTITY: "passed"})
    patched = summary({ADD: "passed", IDENTITY: "passed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_NO_BUG_DEMONSTRATED
    assert "not demonstrated" in verification.OUTCOME_DESCRIPTIONS[comparison.outcome]


def test_differing_collected_sets_are_not_comparable():
    baseline = summary({ADD: "failed", ALPHA: "passed", BETA: "passed"})
    patched = summary({ADD: "passed", ALPHA: "passed"})
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_COLLECTION_MISMATCH
    assert comparison.missing_from_patched == [BETA]


def test_a_patched_collection_error_is_never_an_improvement():
    """The failing test vanished — because nothing could be imported."""
    baseline = summary({ADD: "failed", IDENTITY: "passed"})
    patched = classify_run(
        make_result(
            make_report({}, collect_errors=[{"nodeid": "tests/test_calc.py", "message": "boom"}]),
            exit_code=2,
        )
    )
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_PATCHED_COLLECTION_ERROR
    assert comparison.fixed == []


# --- Run classification -----------------------------------------------------


@pytest.mark.parametrize(
    "exit_code,expected",
    [
        (0, verification.RUN_OK),
        (1, verification.RUN_TESTS_FAILED),
        (2, verification.RUN_INTERRUPTED),
        (3, verification.RUN_INTERNAL_ERROR),
        (4, verification.RUN_USAGE_ERROR),
        (5, verification.RUN_NO_TESTS),
    ],
)
def test_pytest_exit_codes_are_distinguished(exit_code, expected):
    """Not every non-zero exit is an 'environment limitation'."""
    report = make_report({ADD: "passed"}, exitstatus=exit_code)
    assert classify_run(make_result(report, exit_code=exit_code)).kind == expected


def test_a_timeout_outranks_any_report():
    result = make_result(BASELINE_REPORT, exit_code=None, timed_out=True)
    assert classify_run(result).kind == verification.RUN_TIMEOUT


def test_a_missing_report_is_classified_as_such_not_as_success():
    result = make_result(None, exit_code=0, report_error="nothing written")
    classified = classify_run(result)
    assert classified.kind == verification.RUN_NO_REPORT
    assert not classified.usable


def test_exit_zero_with_nothing_collected_is_not_evidence():
    report = make_report({}, collected=[], exitstatus=0)
    assert classify_run(make_result(report, exit_code=0)).kind == verification.RUN_NO_TESTS


# --- End-to-end orchestration ------------------------------------------------


def test_a_correct_patch_demonstrates_a_fix(tmp_path):
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "comparison": make_result(
                make_report({ADD: "passed", IDENTITY: "passed", ALPHA: "passed", BETA: "passed"}),
                exit_code=0,
            ),
        }
    )
    result, events, _ = verify(fx.correct_patch(), runner_script, tmp_path)

    assert result.outcome == OUTCOME_FIX_DEMONSTRATED
    assert result.patch_applied is True
    assert result.files_changed == ["calc/__init__.py"]
    assert runner_script.workspaces == ["baseline", "comparison"]
    assert [kind for kind, _ in events][:2] == ["snapshot_started", "snapshot_ready"]


def test_a_patch_that_does_not_apply_is_reported_with_its_baseline(tmp_path):
    runner_script = ScriptedRunner({"baseline": make_result(BASELINE_REPORT)})
    result, _, _ = verify(fx.non_applying_patch(), runner_script, tmp_path)

    assert result.outcome == OUTCOME_PATCH_DID_NOT_APPLY
    assert result.patch_applied is False
    assert "does not apply" in result.apply_message
    # The baseline still ran, so the user learns something.
    assert result.baseline is not None and len(result.baseline.failing) == 1
    assert runner_script.workspaces == ["baseline"]


def test_the_users_checkout_is_never_touched(tmp_path):
    """Everything happens in runner-owned workspaces."""
    runner_script = ScriptedRunner(
        {"baseline": make_result(BASELINE_REPORT),
         "comparison": make_result(make_report({n: "passed" for n in ALL_TESTS}), exit_code=0)}
    )
    before = (fx.SAMPLE_REPO / fx.CALC).read_text()
    verify(fx.correct_patch(), runner_script, tmp_path)
    assert (fx.SAMPLE_REPO / fx.CALC).read_text() == before


def test_deleting_the_failing_test_cannot_manufacture_success(tmp_path):
    """The classic yardstick attack: remove the test that proves the bug."""
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "patched_full": make_result(
                make_report({IDENTITY: "passed", ALPHA: "passed", BETA: "passed"}, exitstatus=0),
                exit_code=0,
            ),
        }
    )
    result, _, root = verify(fx.deletes_failing_test_patch(), runner_script, tmp_path)

    # The deleted test was restored for the comparison run.
    assert "tests/test_calc.py" in result.restored_paths
    restored = os.path.join(root, "comparison", "tests", "test_calc.py")
    assert os.path.isfile(restored)
    assert restored != ""
    assert (fx.SAMPLE_REPO / fx.TEST_FILE).read_text() == open(restored).read()
    # It changed only tests, so no source fix could have been demonstrated.
    assert result.outcome == OUTCOME_TESTS_ONLY_PATCH


def test_a_patch_adding_configuration_cannot_change_the_comparison(tmp_path):
    """An added conftest.py that would deselect the failing test is removed."""
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "patched_full": make_result(make_report({}, collected=[], exitstatus=5), exit_code=5),
        }
    )
    result, _, root = verify(fx.adds_conftest_patch(), runner_script, tmp_path)

    assert "conftest.py" in result.removed_paths
    assert not os.path.exists(os.path.join(root, "comparison", "conftest.py"))
    # It survives in the untouched patched copy used for supplemental evidence.
    assert os.path.exists(os.path.join(root, "patched_full", "conftest.py"))


def test_supplemental_tests_run_against_the_untouched_patched_copy(tmp_path):
    """A patch touching source *and* tests: the comparison uses the original
    tests, while the patch's own new test runs separately."""
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "comparison": make_result(
                make_report({n: "passed" for n in ALL_TESTS}), exit_code=0
            ),
            "patched_full": make_result(make_report({ADD: "passed"}), exit_code=0),
        }
    )
    diff = fx.correct_patch() + fx.make_add_patch(
        "tests/test_extra.py", "def test_extra():\n    assert True\n"
    )
    result, _, root = verify(diff, runner_script, tmp_path)

    assert runner_script.workspaces == ["baseline", "comparison", "patched_full"]
    assert result.outcome == OUTCOME_FIX_DEMONSTRATED
    # The patch's own test exists only in the untouched copy.
    assert os.path.exists(os.path.join(root, "patched_full", "tests", "test_extra.py"))
    assert not os.path.exists(os.path.join(root, "comparison", "tests", "test_extra.py"))


def test_a_supplemental_timeout_does_not_overwrite_the_comparison(tmp_path):
    """Supplemental evidence is strictly separate from the verdict."""
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "comparison": make_result(
                make_report({n: "passed" for n in ALL_TESTS}), exit_code=0
            ),
            "patched_full": make_result(None, exit_code=None, timed_out=True),
        }
    )
    # A patch touching both source and tests: source fix plus its own new test.
    diff = fx.correct_patch() + fx.make_add_patch(
        "tests/test_extra.py", "def test_extra():\n    assert True\n"
    )
    result, _, _ = verify(diff, runner_script, tmp_path)

    assert result.outcome == OUTCOME_FIX_DEMONSTRATED
    assert result.supplemental is not None
    assert result.supplemental.kind == verification.RUN_TIMEOUT


def test_a_baseline_timeout_is_reported_as_a_timeout(tmp_path):
    runner_script = ScriptedRunner(
        {"baseline": make_result(None, exit_code=None, timed_out=True)}
    )
    result, _, _ = verify(fx.correct_patch(), runner_script, tmp_path)
    assert result.outcome == OUTCOME_TIMEOUT


def test_a_repository_without_tests_is_unsupported(tmp_path):
    def no_tests(*, owner, repo, commit_sha, destination):
        os.makedirs(os.path.join(destination, "pkg"))
        with open(os.path.join(destination, "pkg", "thing.py"), "w") as handle:
            handle.write("x = 1\n")
        return SnapshotResult(
            commit_sha=commit_sha, root=destination, file_count=1,
            decompressed_bytes=10, download_bytes=10,
        )

    runner_script = ScriptedRunner({})
    result, _, _ = verify(
        fx.correct_patch(), runner_script, tmp_path, snapshot_fetcher=no_tests
    )
    assert result.outcome == OUTCOME_UNSUPPORTED_LAYOUT
    assert runner_script.workspaces == []


def test_a_tests_only_patch_is_labelled_and_runs_no_comparison(tmp_path):
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "patched_full": make_result(make_report({ADD: "passed"}), exit_code=0),
        }
    )
    diff = fx.make_add_patch("tests/test_new.py", "def test_new():\n    assert True\n")
    result, _, _ = verify(diff, runner_script, tmp_path)
    assert result.outcome == OUTCOME_TESTS_ONLY_PATCH
    # No comparison run: it would be byte-identical to the baseline.
    assert "comparison" not in runner_script.workspaces


# --- Test-environment classification ----------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_calc.py",
        "tests/conftest.py",
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "tests/data/fixture.json",
        "test_root_level.py",
        "thing_test.py",
    ],
)
def test_test_environment_paths_are_recognised(path):
    assert verification.is_test_environment_path(path)


@pytest.mark.parametrize("path", ["calc/__init__.py", "src/pkg/module.py", "README.md"])
def test_source_paths_are_not_treated_as_test_environment(path):
    assert not verification.is_test_environment_path(path)


# --- Incomplete reports ------------------------------------------------------


def test_a_truncated_baseline_report_cannot_produce_a_fix(tmp_path):
    """An incomplete report describes part of the suite, so it proves nothing.

    Missing, malformed, and oversized reports are rejected in the runner; a report
    the plugin itself capped reaches the comparison, and must be refused there.
    """
    baseline = classify_run(
        make_result(make_report({ADD: "failed", IDENTITY: "passed"}, truncated=True))
    )
    patched = classify_run(
        make_result(make_report({ADD: "passed", IDENTITY: "passed"}), exit_code=0)
    )
    assert baseline.truncated is True

    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_INCONCLUSIVE
    assert "truncated" in comparison.detail
    assert "baseline" in comparison.detail
    assert comparison.fixed == []


def test_a_truncated_patched_report_is_also_refused(tmp_path):
    baseline = classify_run(make_result(make_report({ADD: "failed", IDENTITY: "passed"})))
    patched = classify_run(
        make_result(make_report({ADD: "passed", IDENTITY: "passed"}, truncated=True), exit_code=0)
    )
    comparison = compare_runs(baseline, patched)
    assert comparison.outcome == OUTCOME_INCONCLUSIVE
    assert "patched" in comparison.detail


def test_both_truncated_reports_are_named(tmp_path):
    baseline = classify_run(make_result(make_report({ADD: "failed"}, truncated=True)))
    patched = classify_run(make_result(make_report({ADD: "passed"}, truncated=True), exit_code=0))
    detail = compare_runs(baseline, patched).detail
    assert "baseline and patched" in detail


def test_a_tests_only_patch_still_collects_supplemental_evidence(tmp_path):
    """With no source change, the patch's own tests are the only evidence there is."""
    runner_script = ScriptedRunner(
        {
            "baseline": make_result(BASELINE_REPORT),
            "patched_full": make_result(
                make_report({"tests/test_new.py::test_new": "passed"}), exit_code=0
            ),
        }
    )
    diff = fx.make_add_patch("tests/test_new.py", "def test_new():\n    assert True\n")
    result, _, _ = verify(diff, runner_script, tmp_path)

    assert result.outcome == OUTCOME_TESTS_ONLY_PATCH
    assert runner_script.workspaces == ["baseline", "patched_full"]
    assert result.supplemental is not None
    assert result.supplemental.kind == verification.RUN_OK
    # The verdict is unchanged by that evidence.
    assert result.comparison is None
