"""Cross-attempt comparison: pure functions, no database, no Docker.

The property under test is conservatism. Only a verification that demonstrated a
fix — and provably belongs to the candidate's exact patch on the inspected
commit — can be recommended. Everything else, however close, is not.
"""

from __future__ import annotations

import copy

from app import comparison
from app.comparison import Candidate, changed_lines, evaluate, patch_fingerprint
from tests import fixture_support as fx

SHA = "a" * 40
IMAGE = "sha256:image"
PROFILE = "python-pytest"
ADD = "tests/test_calc.py::test_add"
IDENTITY = "tests/test_calc.py::test_identity"

BASELINE = {
    "kind": "tests_failed", "collected": [ADD, IDENTITY],
    "outcomes": {ADD: "failed", IDENTITY: "passed"},
    "truncated": False, "collect_errors": [],
}
PATCHED = {
    "kind": "ok", "collected": [ADD, IDENTITY],
    "outcomes": {ADD: "passed", IDENTITY: "passed"},
    "truncated": False, "collect_errors": [],
}
EMPTY_COMPARISON = {
    "outcome": "fix_demonstrated", "fixed": [ADD], "still_failing": [], "regressions": [],
    "no_longer_exercised": [], "weakened": [], "missing_from_patched": [],
    "added_in_patched": [], "detail": "",
}


def demonstrated(index: int, diff: str | None = None, **overrides) -> Candidate:
    """A candidate whose evidence genuinely supports a recommendation."""
    diff = diff if diff is not None else fx.correct_patch()
    record = {
        "status": "completed",
        "outcome": "fix_demonstrated",
        "commit_sha": SHA,
        "patch_sha256": patch_fingerprint(diff),
        "profile": PROFILE,
        "image_id": IMAGE,
        "patch_applied": True,
        "comparison": copy.deepcopy(EMPTY_COMPARISON),
        "baseline_summary": copy.deepcopy(BASELINE),
        "patched_summary": copy.deepcopy(PATCHED),
    }
    record.update(overrides)
    return Candidate(
        attempt_index=index, attempt_id=f"attempt-{index}", attempt_status="succeeded",
        diff=diff, commit_sha=SHA, verification=record,
    )


def judge(candidates, *, complete=True):
    return evaluate(candidates, commit_sha=SHA, image_id=IMAGE, profile=PROFILE, complete=complete)


def row(result, index):
    return next(r for r in result["candidates"] if r["attempt_index"] == index)


def test_a_demonstrated_fix_is_recommended():
    result = judge([demonstrated(1)])
    assert result["recommended_attempt_index"] == 1
    assert result["headline"] == "Recommended: attempt 1"
    assert result["recommendation_scope"] == "all_attempts"
    assert row(result, 1)["eligible"] is True and row(result, 1)["reasons"] == []


def test_nothing_is_recommended_when_nothing_qualifies():
    """The least-bad patch is never promoted."""
    partial = demonstrated(1, outcome="partial_fix")
    partial.verification["comparison"]["still_failing"] = ["tests/x::y"]
    failed = Candidate(2, "attempt-2", "failed", None, SHA)
    result = judge([partial, failed])
    assert result["recommended_attempt_index"] is None
    assert result["recommended_attempt_id"] is None
    assert result["headline"] == comparison.HEADLINE_NONE == "No demonstrated fix"


def test_partial_fix_is_not_eligible():
    candidate = demonstrated(1, outcome="partial_fix")
    candidate.verification["comparison"]["still_failing"] = ["tests/x::y"]
    reasons = row(judge([candidate]), 1)["reasons"]
    assert any("partial_fix" in r for r in reasons)


def test_the_evidence_is_rechecked_not_just_the_outcome_string():
    """A record claiming fix_demonstrated but listing a regression is rejected."""
    candidate = demonstrated(1)
    candidate.verification["comparison"]["regressions"] = [IDENTITY]
    result = judge([candidate])
    assert result["recommended_attempt_index"] is None
    assert any("now fail" in r for r in row(result, 1)["reasons"])


def test_a_mismatched_patch_hash_is_rejected():
    candidate = demonstrated(1, patch_sha256="0" * 64)
    result = judge([candidate])
    assert result["recommended_attempt_index"] is None
    assert any("patch hash" in r for r in row(result, 1)["reasons"])


def test_a_mismatched_commit_is_rejected():
    candidate = demonstrated(1, commit_sha="b" * 40)
    result = judge([candidate])
    assert result["recommended_attempt_index"] is None
    assert any("commit" in r for r in row(result, 1)["reasons"])


def test_a_different_image_or_profile_is_rejected():
    assert any(
        "image" in r for r in row(judge([demonstrated(1, image_id="sha256:other")]), 1)["reasons"]
    )
    assert any(
        "profile" in r for r in row(judge([demonstrated(1, profile="other")]), 1)["reasons"]
    )


def test_every_weakened_yardstick_disqualifies():
    for key in ("no_longer_exercised", "weakened", "missing_from_patched", "added_in_patched"):
        candidate = demonstrated(1)
        candidate.verification["comparison"][key] = ["tests/x::y"]
        assert judge([candidate])["recommended_attempt_index"] is None, key


def test_truncated_reports_and_collection_problems_disqualify():
    truncated = demonstrated(1)
    truncated.verification["patched_summary"]["truncated"] = True
    collect = demonstrated(2)
    collect.verification["patched_summary"]["kind"] = "collection_error"
    errors = demonstrated(3)
    errors.verification["baseline_summary"]["collect_errors"] = [{"nodeid": "x", "message": "y"}]
    result = judge([truncated, collect, errors])
    assert result["recommended_attempt_index"] is None
    assert all(not r["eligible"] for r in result["candidates"])


def test_other_outcomes_are_never_eligible():
    for outcome in (
        "collection_mismatch", "patched_collection_error", "tests_only_patch",
        "still_failing", "regressions", "inconclusive", "timeout",
    ):
        assert judge([demonstrated(1, outcome=outcome)])["recommended_attempt_index"] is None, outcome


def test_failed_or_interrupted_verifications_are_not_eligible():
    for status in ("failed", "interrupted", "running"):
        result = judge([demonstrated(1, status=status)])
        assert result["recommended_attempt_index"] is None
        assert any("did not complete" in r for r in row(result, 1)["reasons"])


def test_a_missing_verification_or_incomplete_pipeline_is_not_eligible():
    no_verification = Candidate(
        1, "attempt-1", "succeeded", fx.correct_patch(), SHA,
        pipeline_error_kind="verification_not_started",
        pipeline_error_message="The patch was proposed and saved, but its verification never started",
        verification=None,
    )
    reasons = row(judge([no_verification]), 1)["reasons"]
    assert any("verification_not_started" in r for r in reasons)
    assert any("No verification" in r for r in reasons)


def test_supplemental_results_are_never_read():
    """A failing supplemental run does not disqualify; a passing one does not qualify."""
    eligible = demonstrated(1, supplemental_summary={"kind": "tests_failed"})
    tests_only = demonstrated(2, outcome="tests_only_patch", supplemental_summary={"kind": "ok"})
    result = judge([eligible, tests_only])
    assert row(result, 1)["eligible"] is True
    assert row(result, 2)["eligible"] is False
    assert result["recommended_attempt_index"] == 1


def test_the_tie_breaker_prefers_fewer_changed_lines_then_the_lower_index():
    small = fx.correct_patch()
    large = fx.noisy_patch()
    assert changed_lines(small) < changed_lines(large)

    result = judge([demonstrated(1, diff=large), demonstrated(2, diff=small)])
    assert result["recommended_attempt_index"] == 2
    assert "not evidence of better code" in result["tie_breaker"]

    tied = judge([demonstrated(3, diff=small), demonstrated(2, diff=small)])
    assert tied["recommended_attempt_index"] == 2


def test_disagreeing_baselines_block_a_cross_attempt_recommendation():
    first = demonstrated(1)
    second = demonstrated(2, diff=fx.noisy_patch())
    second.verification["baseline_summary"]["outcomes"][IDENTITY] = "failed"
    result = judge([first, second])
    assert result["baselines_consistent"] is False
    assert result["recommended_attempt_index"] is None
    assert "baselines disagree" in result["headline"]
    assert any("disagree about the baseline" in n for n in result["notes"])


def test_an_incomplete_orchestration_scopes_its_recommendation():
    result = judge([demonstrated(1)], complete=False)
    assert result["recommended_attempt_index"] == 1
    assert result["recommendation_scope"] == "completed_attempts_only"
    assert result["headline"].startswith("Recommended among completed attempts only")
    assert result["complete"] is False


def test_changed_lines_counts_only_hunk_bodies():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n--- not a header\n+++ also not\n c\n"
    )
    # One removed line ("-- not a header") and one added ("++ also not").
    assert changed_lines(diff) == 2
