"""Orchestration: baseline, apply, comparison, and an honest verdict.

The purpose of this module is to make "the tests pass" mean something specific,
because the interesting failure mode of a patch-verifying system is not a crash —
it is confidently reporting a fix that is not a fix.

Four guards do that work:

1. **A fix requires an explicit pass.** A previously failing test counts as fixed
   only when that exact node ID reports `passed` afterwards. Vanishing from the
   failing set is not a fix: skipped, xfailed, deselected, and uncollected are all
   recorded as *not fixed*.
2. **Collected identities must match.** The comparison run must collect exactly
   the same node IDs as the baseline. Otherwise the two runs are not comparable
   and the result is inconclusive, however green it looks.
3. **The original test environment is restored** over the patched source before
   the comparison run — tests, every `conftest.py`, fixtures and data files, and
   collection configuration — and any test-environment file the patch *added* is
   removed. A patch cannot alter the yardstick it is measured against.
4. **A missing or malformed report is an error**, never an absence of failures.

This bounds what can be claimed, and the claim stays narrow: the exercised tests
behaved a certain way on this commit. It is not proof of general correctness, and
it is not protection against a patch deliberately engineered to forge results.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app import patch_validation, runner, snapshot
from app.config import Settings
from app.inspection import parse_repository_url
from app.runner import RunnerLimits, RunResult

# --- Run classification -----------------------------------------------------
# pytest's documented exit codes, kept distinct: grouping them all into
# "environment limitation" would hide a usage error behind a plausible excuse.
RUN_OK = "ok"
RUN_TESTS_FAILED = "tests_failed"
RUN_INTERRUPTED = "interrupted"
RUN_INTERNAL_ERROR = "internal_error"
RUN_USAGE_ERROR = "usage_error"
RUN_NO_TESTS = "no_tests_collected"
RUN_COLLECTION_ERROR = "collection_error"
RUN_TIMEOUT = "timeout"
RUN_NO_REPORT = "no_report"

_EXIT_CODE_KINDS = {
    0: RUN_OK,
    1: RUN_TESTS_FAILED,
    2: RUN_INTERRUPTED,
    3: RUN_INTERNAL_ERROR,
    4: RUN_USAGE_ERROR,
    5: RUN_NO_TESTS,
}

# Kinds where the tests actually ran and their outcomes mean something.
_USABLE_KINDS = {RUN_OK, RUN_TESTS_FAILED}

# --- Verification outcomes --------------------------------------------------
# Deliberately specific. There is no generic "verified" value, because a single
# pass/fail field is exactly what decays into a misleading badge.
OUTCOME_FIX_DEMONSTRATED = "fix_demonstrated"
OUTCOME_PARTIAL_FIX = "partial_fix"
OUTCOME_STILL_FAILING = "still_failing"
OUTCOME_REGRESSIONS = "regressions"
OUTCOME_NO_BUG_DEMONSTRATED = "no_bug_demonstrated"
OUTCOME_TESTS_ONLY_PATCH = "tests_only_patch"
OUTCOME_PATCH_DID_NOT_APPLY = "patch_did_not_apply"
OUTCOME_COLLECTION_MISMATCH = "collection_mismatch"
OUTCOME_PATCHED_COLLECTION_ERROR = "patched_collection_error"
OUTCOME_BASELINE_UNUSABLE = "baseline_unusable"
OUTCOME_UNSUPPORTED_LAYOUT = "unsupported_layout"
OUTCOME_ENVIRONMENT_LIMITATION = "environment_limitation"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_INCONCLUSIVE = "inconclusive"

# Human-readable, and the only place this wording is defined. The UI renders
# these verbatim so the copy cannot drift toward "verified fix".
OUTCOME_DESCRIPTIONS = {
    OUTCOME_FIX_DEMONSTRATED: (
        "Every originally failing test now passes and no other test regressed. "
        "This demonstrates the exercised tests changed behaviour — it is not proof "
        "the patch is correct in general."
    ),
    OUTCOME_PARTIAL_FIX: "Some originally failing tests now pass, but not all of them.",
    OUTCOME_STILL_FAILING: "The originally failing tests still do not pass.",
    OUTCOME_REGRESSIONS: "The patch made previously passing tests fail.",
    OUTCOME_NO_BUG_DEMONSTRATED: (
        "Existing tests pass; bug fix not demonstrated. No original test failed "
        "before the patch, so there was nothing for it to fix."
    ),
    OUTCOME_TESTS_ONLY_PATCH: (
        "The patch changes only tests or test configuration, so the comparison run "
        "would be identical to the baseline. No source fix was demonstrated; the "
        "patch's own tests are reported separately as supplemental evidence."
    ),
    OUTCOME_PATCH_DID_NOT_APPLY: "The patch did not apply to the inspected commit.",
    OUTCOME_COLLECTION_MISMATCH: (
        "The patched run collected a different set of tests than the baseline, so "
        "the two runs cannot be compared."
    ),
    OUTCOME_PATCHED_COLLECTION_ERROR: (
        "The patched source failed to import or collect. This is a defect in the "
        "patch, not an improvement — the tests never ran."
    ),
    OUTCOME_BASELINE_UNUSABLE: "The baseline run did not produce usable results.",
    OUTCOME_UNSUPPORTED_LAYOUT: (
        "No original pytest suite could be identified in this repository, so there "
        "is nothing to compare against."
    ),
    OUTCOME_ENVIRONMENT_LIMITATION: (
        "The repository needs dependencies or setup the runner profile does not "
        "provide. This says nothing about whether the patch is correct."
    ),
    OUTCOME_TIMEOUT: "A test run exceeded its wall-clock limit and was terminated.",
    OUTCOME_INCONCLUSIVE: "The runs completed but could not be compared reliably.",
}

# --- Original test environment ----------------------------------------------
# Files that define *how* tests are collected and run, and therefore what the
# comparison measures. Restoring these is what stops a patch from moving the
# goalposts.
_TEST_CONFIG_NAMES = {
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
}
_TEST_DIR_NAMES = {"tests", "test", "testing"}


def is_test_environment_path(relative_path: str) -> bool:
    """True if this path is part of the test environment rather than the source."""
    parts = relative_path.replace("\\", "/").split("/")
    name = parts[-1]
    if name in _TEST_CONFIG_NAMES:
        return True
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    # Everything inside a tests directory: fixtures, data files, __init__.py.
    return any(part in _TEST_DIR_NAMES for part in parts[:-1])


def is_collectible_test_file(relative_path: str) -> bool:
    """True if pytest would collect this file by its default naming rules."""
    name = relative_path.replace("\\", "/").split("/")[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _relative_paths(root: str) -> set[str]:
    found: set[str] = set()
    for current, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(current, name)
            found.add(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def restore_original_test_environment(
    *, pristine: str, target: str
) -> tuple[list[str], list[str]]:
    """Copy the original test environment over `target` and drop patch-added ones.

    Returns (restored, removed). Both directions matter: restoring alone would
    leave a patch-added `conftest.py` free to change collection.
    """
    pristine_paths = _relative_paths(pristine)
    target_paths = _relative_paths(target)

    restored: list[str] = []
    for relative in sorted(pristine_paths):
        if not is_test_environment_path(relative):
            continue
        source = os.path.join(pristine, *relative.split("/"))
        destination = os.path.join(target, *relative.split("/"))
        os.makedirs(os.path.dirname(destination), mode=0o755, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o644)
        restored.append(relative)

    removed: list[str] = []
    for relative in sorted(target_paths - pristine_paths):
        if not is_test_environment_path(relative):
            continue
        os.remove(os.path.join(target, *relative.split("/")))
        removed.append(relative)

    return restored, removed


# --- Patch application ------------------------------------------------------


@dataclass
class ApplyResult:
    applied: bool
    message: str
    files_changed: list[str] = field(default_factory=list)
    touched_test_environment: bool = False


def _git_environment(ceiling: str) -> dict[str, str]:
    """A deterministic environment for `git apply`.

    Every inherited `GIT_*` variable is dropped, config files are pointed at
    /dev/null, and `GIT_CEILING_DIRECTORIES` stops repository discovery from
    walking up into whatever repository happens to contain the temp directory.
    Without this, the developer's own git config could change whitespace handling
    and make the persisted runner arguments non-reproducible.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CEILING_DIRECTORIES": ceiling,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": ceiling,
        }
    )
    return env


def apply_patch(
    diff: str,
    *,
    workspace: str,
    ceiling: str,
    max_patch_bytes: int,
    timeout_seconds: float = 60.0,
    max_output_chars: int = 4_000,
) -> ApplyResult:
    """Validate then apply `diff` inside `workspace` using git.

    Revalidates the diff at this boundary even though it was validated when the
    model submitted it: this is where it stops being data and starts touching a
    filesystem.
    """
    try:
        files = patch_validation.validate_unified_diff(diff, max_bytes=max_patch_bytes)
    except patch_validation.PatchValidationError as error:
        return ApplyResult(applied=False, message=f"The stored patch is not valid: {error}")

    changed = [f.display_path for f in files]
    touched_tests = any(is_test_environment_path(path) for path in changed)

    handle, patch_path = tempfile.mkstemp(suffix=".diff", dir=ceiling)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(diff if diff.endswith("\n") else diff + "\n")

        env = _git_environment(ceiling)
        base_args = ["git", "apply", "-p1", "--whitespace=nowarn"]

        for stage in ("--check", None):
            args = list(base_args)
            if stage:
                args.append(stage)
            args.append(patch_path)
            try:
                completed = subprocess.run(
                    args,
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return ApplyResult(
                    applied=False,
                    message=f"`git apply` exceeded {timeout_seconds:g}s and was terminated.",
                    files_changed=changed,
                    touched_test_environment=touched_tests,
                )
            except FileNotFoundError:
                return ApplyResult(
                    applied=False, message="`git` was not found on PATH.", files_changed=changed
                )

            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                return ApplyResult(
                    applied=False,
                    message=(
                        f"The patch does not apply to this commit: "
                        f"{detail[:max_output_chars]}"
                    ),
                    files_changed=changed,
                    touched_test_environment=touched_tests,
                )
    finally:
        try:
            os.remove(patch_path)
        except OSError:  # pragma: no cover
            pass

    return ApplyResult(
        applied=True,
        message=f"Applied cleanly to {len(changed)} file(s).",
        files_changed=changed,
        touched_test_environment=touched_tests,
    )


# --- Run classification and comparison --------------------------------------


@dataclass
class RunSummary:
    """A classified test run, safe to persist and to compare."""

    kind: str
    exit_code: int | None
    collected: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    collect_errors: list[dict[str, str]] = field(default_factory=list)
    report_error: str | None = None
    truncated: bool = False
    duration_seconds: float = 0.0
    timed_out: bool = False

    @property
    def usable(self) -> bool:
        """True when the tests actually ran and their outcomes mean something."""
        return self.kind in _USABLE_KINDS

    @property
    def failing(self) -> set[str]:
        return {n for n, o in self.outcomes.items() if o in ("failed", "error")}

    @property
    def passing(self) -> set[str]:
        return {n for n, o in self.outcomes.items() if o == "passed"}

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "exit_code": self.exit_code,
            "collected": self.collected,
            "outcomes": self.outcomes,
            "counts": self.counts,
            "collect_errors": self.collect_errors,
            "report_error": self.report_error,
            "truncated": self.truncated,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
        }


def classify_run(result: RunResult) -> RunSummary:
    """Turn a raw container run into a classified summary.

    Order matters. A timeout or a missing report outranks the exit code, and
    collection errors outrank "tests failed" — a suite that could not be imported
    did not fail its tests, it never ran them, and calling that a test failure is
    how a broken patch gets mistaken for a fixed one.
    """
    if result.timed_out:
        return RunSummary(
            kind=RUN_TIMEOUT,
            exit_code=result.exit_code,
            report_error=result.report_error,
            duration_seconds=result.duration_seconds,
            timed_out=True,
        )

    if result.report is None:
        return RunSummary(
            kind=RUN_NO_REPORT,
            exit_code=result.exit_code,
            report_error=result.report_error or "No structured report was produced.",
            duration_seconds=result.duration_seconds,
        )

    report = result.report
    collected = [str(n) for n in report.get("collected", [])]
    raw_tests = report.get("tests", {})
    outcomes = {
        str(node): str(entry.get("outcome", "error"))
        for node, entry in raw_tests.items()
        if isinstance(entry, dict)
    }
    collect_errors = [
        {"nodeid": str(e.get("nodeid", "")), "message": str(e.get("message", ""))}
        for e in report.get("collect_errors", [])
        if isinstance(e, dict)
    ]
    counts = {str(k): int(v) for k, v in report.get("counts", {}).items()}

    if collect_errors:
        kind = RUN_COLLECTION_ERROR
    else:
        kind = _EXIT_CODE_KINDS.get(result.exit_code or 0, RUN_INTERNAL_ERROR)
        # An exit code of 0 with nothing collected still means no evidence.
        if kind == RUN_OK and not collected:
            kind = RUN_NO_TESTS

    return RunSummary(
        kind=kind,
        exit_code=result.exit_code,
        collected=collected,
        outcomes=outcomes,
        counts=counts,
        collect_errors=collect_errors,
        report_error=result.report_error,
        truncated=bool(report.get("truncated")),
        duration_seconds=result.duration_seconds,
    )


@dataclass
class Comparison:
    outcome: str
    fixed: list[str] = field(default_factory=list)
    still_failing: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    no_longer_exercised: list[str] = field(default_factory=list)
    weakened: list[str] = field(default_factory=list)
    missing_from_patched: list[str] = field(default_factory=list)
    added_in_patched: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "fixed": self.fixed,
            "still_failing": self.still_failing,
            "regressions": self.regressions,
            "no_longer_exercised": self.no_longer_exercised,
            "weakened": self.weakened,
            "missing_from_patched": self.missing_from_patched,
            "added_in_patched": self.added_in_patched,
            "detail": self.detail,
        }


def compare_runs(baseline: RunSummary, patched: RunSummary) -> Comparison:
    """Compare a baseline against a patched run and name the outcome."""
    if not baseline.usable:
        if baseline.kind == RUN_TIMEOUT:
            return Comparison(outcome=OUTCOME_TIMEOUT, detail="The baseline run timed out.")
        if baseline.kind in (RUN_COLLECTION_ERROR, RUN_NO_TESTS):
            return Comparison(
                outcome=OUTCOME_ENVIRONMENT_LIMITATION,
                detail=(
                    f"The baseline run could not collect the suite ({baseline.kind}). "
                    f"The repository likely needs dependencies the runner profile "
                    f"does not provide."
                ),
            )
        return Comparison(
            outcome=OUTCOME_BASELINE_UNUSABLE,
            detail=f"The baseline run ended as {baseline.kind}.",
        )

    if patched.kind == RUN_TIMEOUT:
        return Comparison(outcome=OUTCOME_TIMEOUT, detail="The patched run timed out.")
    if patched.kind == RUN_COLLECTION_ERROR:
        # The crucial asymmetry: the baseline collected fine, so this is the
        # patch's doing. Tests disappearing from the failing set here is a
        # symptom of breakage, never an improvement.
        return Comparison(
            outcome=OUTCOME_PATCHED_COLLECTION_ERROR,
            detail=(
                "The baseline collected successfully but the patched source did not. "
                "The patch breaks import or collection."
            ),
        )
    if not patched.usable:
        return Comparison(
            outcome=OUTCOME_INCONCLUSIVE,
            detail=f"The patched run ended as {patched.kind}.",
        )

    # An incomplete report describes only part of the suite, so neither the
    # collected-identity check nor the per-test comparison below can be trusted.
    # Missing, malformed, and oversized reports fail earlier; this is the fourth
    # case — a report the plugin itself capped.
    if baseline.truncated or patched.truncated:
        which = " and ".join(
            name
            for name, summary in (("baseline", baseline), ("patched", patched))
            if summary.truncated
        )
        return Comparison(
            outcome=OUTCOME_INCONCLUSIVE,
            detail=(
                f"The {which} report was truncated because the suite exceeded the "
                f"reporting cap, so only part of it is recorded. Refusing to compare "
                f"an incomplete picture."
            ),
        )

    baseline_ids = set(baseline.collected)
    patched_ids = set(patched.collected)
    if baseline_ids != patched_ids:
        missing = sorted(baseline_ids - patched_ids)
        added = sorted(patched_ids - baseline_ids)
        return Comparison(
            outcome=OUTCOME_COLLECTION_MISMATCH,
            missing_from_patched=missing[:50],
            added_in_patched=added[:50],
            detail=(
                f"The patched run collected a different suite: {len(missing)} test(s) "
                f"missing, {len(added)} added. The runs are not comparable."
            ),
        )

    baseline_failing = baseline.failing
    baseline_passing = baseline.passing

    # A fix requires this exact node ID to report `passed`. Skipped, xfailed,
    # deselected, or absent are all "not fixed".
    fixed = sorted(n for n in baseline_failing if patched.outcomes.get(n) == "passed")
    still_failing = sorted(
        n for n in baseline_failing if patched.outcomes.get(n) in ("failed", "error")
    )
    no_longer_exercised = sorted(
        n
        for n in baseline_failing
        if patched.outcomes.get(n) not in ("passed", "failed", "error")
    )
    regressions = sorted(
        n for n in baseline_passing if patched.outcomes.get(n) in ("failed", "error")
    )
    # A previously passing test that stopped running is a weakened yardstick.
    weakened = sorted(
        n
        for n in baseline_passing
        if patched.outcomes.get(n) not in ("passed", "failed", "error")
    )

    if not baseline_failing:
        outcome = OUTCOME_NO_BUG_DEMONSTRATED
        detail = (
            f"All {len(baseline_passing)} original test(s) already passed before the "
            f"patch, so no failing behaviour was available to fix."
        )
        if regressions:
            outcome = OUTCOME_REGRESSIONS
            detail = f"The patch broke {len(regressions)} previously passing test(s)."
        return Comparison(
            outcome=outcome, regressions=regressions, weakened=weakened, detail=detail
        )

    if regressions:
        outcome = OUTCOME_REGRESSIONS
        detail = (
            f"{len(regressions)} previously passing test(s) now fail. "
            f"{len(fixed)} of {len(baseline_failing)} originally failing test(s) now pass."
        )
    elif weakened:
        outcome = OUTCOME_INCONCLUSIVE
        detail = (
            f"{len(weakened)} previously passing test(s) no longer run under the "
            f"patched source, so the comparison is not trustworthy."
        )
    elif len(fixed) == len(baseline_failing):
        outcome = OUTCOME_FIX_DEMONSTRATED
        detail = (
            f"All {len(baseline_failing)} originally failing test(s) now pass, and "
            f"the other {len(baseline_passing)} still pass."
        )
    elif fixed:
        outcome = OUTCOME_PARTIAL_FIX
        detail = (
            f"{len(fixed)} of {len(baseline_failing)} originally failing test(s) now "
            f"pass; {len(still_failing) + len(no_longer_exercised)} do not."
        )
    else:
        outcome = OUTCOME_STILL_FAILING
        detail = f"None of the {len(baseline_failing)} originally failing test(s) now pass."

    if no_longer_exercised and outcome in (OUTCOME_FIX_DEMONSTRATED, OUTCOME_PARTIAL_FIX):
        detail += (
            f" Note: {len(no_longer_exercised)} originally failing test(s) no longer "
            f"run (skipped or xfailed) and are not counted as fixed."
        )

    return Comparison(
        outcome=outcome,
        fixed=fixed,
        still_failing=still_failing,
        regressions=regressions,
        no_longer_exercised=no_longer_exercised,
        weakened=weakened,
        detail=detail,
    )


# --- Top-level orchestration ------------------------------------------------


class SnapshotFetcher(Protocol):
    def __call__(
        self, *, owner: str, repo: str, commit_sha: str, destination: str
    ) -> snapshot.SnapshotResult: ...


class TestRunner(Protocol):
    def __call__(self, *, workspace: str, results_dir: str, image_id: str) -> RunResult: ...


@dataclass
class VerificationResult:
    """Everything worth persisting from one verification."""

    outcome: str
    detail: str
    patch_applied: bool
    apply_message: str
    files_changed: list[str] = field(default_factory=list)
    patch_touched_tests: bool = False
    restored_paths: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)
    baseline: RunSummary | None = None
    patched: RunSummary | None = None
    supplemental: RunSummary | None = None
    comparison: Comparison | None = None
    baseline_log: str = ""
    patched_log: str = ""
    supplemental_log: str = ""
    runner_args: list[str] = field(default_factory=list)
    snapshot_files: int = 0
    cleanup_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def patch_fingerprint(diff: str) -> str:
    """A stable identity for the exact patch that was verified."""
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def _copy_tree(source: str, destination: str) -> None:
    shutil.copytree(source, destination, symlinks=False)


def _default_snapshot_fetcher(config: Settings) -> SnapshotFetcher:
    limits = snapshot.SnapshotLimits(
        max_download_bytes=config.verify_max_download_bytes,
        max_decompressed_bytes=config.verify_max_decompressed_bytes,
        max_files=config.verify_max_snapshot_files,
        deadline_seconds=config.verify_snapshot_deadline_seconds,
    )

    def fetch(*, owner: str, repo: str, commit_sha: str, destination: str) -> snapshot.SnapshotResult:
        return snapshot.fetch_snapshot(
            owner=owner,
            repo=repo,
            commit_sha=commit_sha,
            destination=destination,
            limits=limits,
        )

    return fetch


def _default_test_runner(config: Settings) -> TestRunner:
    limits = runner_limits_from(config)

    def run(*, workspace: str, results_dir: str, image_id: str) -> RunResult:
        return runner.run_tests(
            workspace=workspace,
            results_dir=results_dir,
            image_id=image_id,
            limits=limits,
            report_max_tests=config.verify_report_max_tests,
            report_max_message_chars=config.verify_report_max_message_chars,
        )

    return run


def runner_limits_from(config: Settings) -> RunnerLimits:
    return RunnerLimits(
        memory=config.verify_container_memory,
        cpus=config.verify_container_cpus,
        pids=config.verify_container_pids,
        tmpfs_bytes=config.verify_container_tmpfs_bytes,
        timeout_seconds=config.verify_test_timeout_seconds,
        max_log_bytes=config.verify_max_log_bytes,
        max_report_bytes=config.verify_max_report_bytes,
        docker_binary=config.verify_docker_binary,
        cleanup_timeout_seconds=config.verify_cleanup_timeout_seconds,
    )


def run_verification(
    *,
    repository_url: str,
    commit_sha: str,
    diff: str,
    config: Settings,
    image_id: str,
    record: Callable[[str, str, str | None], None],
    snapshot_fetcher: SnapshotFetcher | None = None,
    test_runner: TestRunner | None = None,
    workspace_root: str | None = None,
) -> VerificationResult:
    """Snapshot, baseline, apply, compare — and clean up whatever happens.

    Workspaces are runner-owned temporary directories. The user's BranchForge
    checkout is never touched, and nothing is written inside the repository under
    verification except by `git apply` into a throwaway copy.
    """
    snapshot_fetcher = snapshot_fetcher or _default_snapshot_fetcher(config)
    test_runner = test_runner or _default_test_runner(config)

    ref = parse_repository_url(repository_url)
    owned_root = workspace_root is None
    root = os.path.realpath(workspace_root or tempfile.mkdtemp(prefix="branchforge-verify-"))

    result = VerificationResult(
        outcome=OUTCOME_INCONCLUSIVE, detail="", patch_applied=False, apply_message=""
    )

    try:
        # --- 1. One immutable snapshot, copied per phase ---------------------
        snapshot_dir = os.path.join(root, "snapshot")
        record("snapshot_started", f"Fetching source for commit {commit_sha[:12]}", None)
        acquired = snapshot_fetcher(
            owner=ref.owner, repo=ref.repo, commit_sha=commit_sha, destination=snapshot_dir
        )
        result.snapshot_files = acquired.file_count
        record(
            "snapshot_ready",
            f"Snapshot: {acquired.file_count:,} files at {commit_sha[:12]}",
            None,
        )

        # --- 2. Is there anything to compare against? ------------------------
        relative_paths = _relative_paths(snapshot_dir)
        if not any(is_collectible_test_file(p) for p in relative_paths):
            result.outcome = OUTCOME_UNSUPPORTED_LAYOUT
            result.detail = OUTCOME_DESCRIPTIONS[OUTCOME_UNSUPPORTED_LAYOUT]
            result.apply_message = "Not attempted: no original test suite was found."
            record("unsupported_layout", "No pytest suite found in the snapshot", None)
            return result

        # --- 3. Baseline, before anything is applied -------------------------
        baseline_dir = os.path.join(root, "baseline")
        _copy_tree(snapshot_dir, baseline_dir)
        record("baseline_started", "Running the original test suite (baseline)", None)
        baseline_raw = test_runner(
            workspace=baseline_dir,
            results_dir=os.path.join(root, "results-baseline"),
            image_id=image_id,
        )
        result.runner_args = baseline_raw.argv
        result.baseline_log = baseline_raw.log
        result.cleanup_errors.extend(baseline_raw.cleanup_errors)
        baseline = classify_run(baseline_raw)
        result.baseline = baseline
        record(
            "baseline_finished",
            f"Baseline: {baseline.kind} ({len(baseline.failing)} failing of "
            f"{len(baseline.collected)} collected)",
            baseline.report_error,
        )

        # --- 4. Apply the patch to its own copy ------------------------------
        patched_full_dir = os.path.join(root, "patched_full")
        _copy_tree(snapshot_dir, patched_full_dir)
        apply_result = apply_patch(
            diff,
            workspace=patched_full_dir,
            ceiling=root,
            max_patch_bytes=config.agent_max_patch_bytes,
            timeout_seconds=config.verify_git_timeout_seconds,
        )
        result.patch_applied = apply_result.applied
        result.apply_message = apply_result.message
        result.files_changed = apply_result.files_changed
        result.patch_touched_tests = apply_result.touched_test_environment

        if not apply_result.applied:
            result.outcome = OUTCOME_PATCH_DID_NOT_APPLY
            result.detail = OUTCOME_DESCRIPTIONS[OUTCOME_PATCH_DID_NOT_APPLY]
            record("patch_rejected", "The patch did not apply", apply_result.message)
            return result

        record(
            "patch_applied",
            f"Patch applied to {len(apply_result.files_changed)} file(s)",
            ", ".join(apply_result.files_changed)[:500],
        )

        # An unusable baseline decides the outcome on its own, so stop here
        # rather than spending another container on a comparison that could not
        # be trusted anyway. Whether the patch applies is already recorded above.
        if not baseline.usable:
            verdict = compare_runs(baseline, baseline)
            result.outcome = verdict.outcome
            result.detail = verdict.detail or OUTCOME_DESCRIPTIONS.get(verdict.outcome, "")
            result.comparison = verdict
            record(
                "baseline_unusable",
                f"Baseline was {baseline.kind}; no comparison is possible",
                verdict.detail,
            )
            return result

        # --- 5. Comparison copy: patched source, ORIGINAL test environment ---
        comparison_dir = os.path.join(root, "comparison")
        _copy_tree(patched_full_dir, comparison_dir)
        restored, removed = restore_original_test_environment(
            pristine=snapshot_dir, target=comparison_dir
        )
        result.restored_paths = restored
        result.removed_paths = removed
        if apply_result.touched_test_environment:
            record(
                "tests_restored",
                f"Patch touched the test environment; restored {len(restored)} original "
                f"file(s) and removed {len(removed)} added one(s) for the comparison",
                None,
            )

        def run_supplemental() -> None:
            """Run the patch's own tests against the untouched patched copy.

            Separate from the verdict in every sense: its own workspace, its own
            columns, and errors swallowed — supplemental evidence must never take
            down or alter a baseline-versus-patched result.
            """
            if not apply_result.touched_test_environment:
                return
            record(
                "supplemental_started",
                "Running the patch's own tests separately (supplemental evidence)",
                None,
            )
            try:
                supplemental_raw = test_runner(
                    workspace=patched_full_dir,
                    results_dir=os.path.join(root, "results-supplemental"),
                    image_id=image_id,
                )
            except runner.RunnerError as error:
                result.notes.append(f"The supplemental run could not complete: {error}")
                record("supplemental_failed", "The supplemental run could not complete", str(error))
                return
            result.supplemental_log = supplemental_raw.log
            result.cleanup_errors.extend(supplemental_raw.cleanup_errors)
            result.supplemental = classify_run(supplemental_raw)
            record(
                "supplemental_finished",
                f"Supplemental (patch's own tests): {result.supplemental.kind}",
                None,
            )

        source_changes = [
            p for p in apply_result.files_changed if not is_test_environment_path(p)
        ]
        if not source_changes:
            # The comparison run would be byte-identical to the baseline.
            result.outcome = OUTCOME_TESTS_ONLY_PATCH
            result.detail = OUTCOME_DESCRIPTIONS[OUTCOME_TESTS_ONLY_PATCH]
            result.notes.append(
                "The patch changed only test or configuration files, so no source "
                "behaviour could have changed."
            )
            record("tests_only_patch", "The patch changes only tests or configuration", None)
            # No source changed, so the comparison would repeat the baseline. The
            # patch's own tests are the only evidence there is — collect it.
            run_supplemental()
            return result

        record("patched_started", "Running the original tests against the patched source", None)
        patched_raw = test_runner(
            workspace=comparison_dir,
            results_dir=os.path.join(root, "results-patched"),
            image_id=image_id,
        )
        result.patched_log = patched_raw.log
        result.cleanup_errors.extend(patched_raw.cleanup_errors)
        patched = classify_run(patched_raw)
        result.patched = patched
        record(
            "patched_finished",
            f"Patched: {patched.kind} ({len(patched.failing)} failing of "
            f"{len(patched.collected)} collected)",
            patched.report_error,
        )

        # --- 6. The verdict --------------------------------------------------
        comparison = compare_runs(baseline, patched)
        result.comparison = comparison
        result.outcome = comparison.outcome
        result.detail = comparison.detail or OUTCOME_DESCRIPTIONS.get(comparison.outcome, "")

        # --- 7. Supplemental: the patch's own tests, kept strictly apart ------
        run_supplemental()

        return result
    finally:
        if owned_root:
            shutil.rmtree(root, ignore_errors=True)
