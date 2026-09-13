"""Comparing competing attempts — conservatively.

Pure functions over plain data: no database, no Docker. The orchestrator gathers
each attempt's persisted facts, calls `evaluate`, and stores what comes back.

The rule is deliberately narrow. An attempt is a candidate for recommendation
only when its own verification *demonstrated* that previously failing original
tests now pass — with nothing missing, nothing regressed, nothing truncated, and
evidence that provably belongs to this attempt's exact patch on the inspected
commit. If no attempt meets that bar the answer is "No demonstrated fix" and no
attempt is recommended: the least-bad patch is never promoted.

Supplemental results (the patch's own tests) are never read here. They are
separate evidence and must not leak into the original-suite verdict.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

FIX_DEMONSTRATED = "fix_demonstrated"
_USABLE_RUN_KINDS = {"ok", "tests_failed"}
_MUST_BE_EMPTY = (
    ("regressions", "previously passing test(s) now fail"),
    ("still_failing", "originally failing test(s) still fail"),
    ("no_longer_exercised", "originally failing test(s) no longer run"),
    ("weakened", "previously passing test(s) no longer run"),
    ("missing_from_patched", "test(s) missing from the patched run"),
    ("added_in_patched", "test(s) appeared only in the patched run"),
)

HEADLINE_NONE = "No demonstrated fix"

ELIGIBILITY_RULE = (
    "An attempt is eligible only if its verification completed with every originally "
    "failing original test now passing and no regressions, missing or skipped tests, "
    "truncated reports, or collection problems — and the verification is provably of "
    "this attempt's exact patch on the inspected commit. Supplemental tests are not "
    "considered."
)
TIE_BREAKER = (
    "Among equally supported candidates, the one with the fewest changed diff lines "
    "is preferred, then the lowest attempt number. This is a preference for smaller "
    "changes, not evidence of better code."
)


@dataclass
class Candidate:
    """One attempt's persisted facts, as the orchestrator read them."""

    attempt_index: int
    attempt_id: str
    attempt_status: str
    diff: str | None
    commit_sha: str | None
    pipeline_error_kind: str | None = None
    pipeline_error_message: str | None = None
    verification: dict[str, Any] | None = None


def patch_fingerprint(diff: str) -> str:
    """Must match `verification.patch_fingerprint`; duplicated to keep this module pure."""
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def changed_lines(diff: str | None) -> int:
    """Added plus removed lines, counted inside hunks only.

    Hunk headers give the exact number of body lines, so a removed line that
    happens to start with `--` is never mistaken for a file header.
    """
    if not diff:
        return 0
    count = 0
    old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker == "-":
                count += 1
                old_left -= 1
            elif marker == "+":
                count += 1
                new_left -= 1
            elif marker == "\\":
                continue
            else:
                old_left -= 1
                new_left -= 1
            continue
        match = _HUNK.match(line)
        if match:
            old_left = int(match.group(1)) if match.group(1) is not None else 1
            new_left = int(match.group(2)) if match.group(2) is not None else 1
    return count


def _baseline_identity(verification: dict[str, Any]) -> tuple:
    baseline = verification.get("baseline_summary") or {}
    collected = tuple(sorted(str(n) for n in baseline.get("collected") or []))
    outcomes = tuple(sorted((str(k), str(v)) for k, v in (baseline.get("outcomes") or {}).items()))
    return collected, outcomes


def _reasons(candidate: Candidate, *, commit_sha: str, image_id: str, profile: str) -> list[str]:
    """Every reason this candidate is not eligible; empty means eligible."""
    reasons: list[str] = []

    if candidate.attempt_status != "succeeded":
        reasons.append(f"The proposal did not produce a patch (attempt {candidate.attempt_status}).")
        return reasons
    if not candidate.diff:
        reasons.append("The attempt has no stored diff.")
        return reasons
    if candidate.pipeline_error_kind:
        reasons.append(
            f"The pipeline did not finish ({candidate.pipeline_error_kind}): "
            f"{candidate.pipeline_error_message or 'no detail recorded'}"
        )

    v = candidate.verification
    if v is None:
        reasons.append("No verification was recorded for this patch.")
        return reasons
    if v.get("status") != "completed":
        reasons.append(f"The verification did not complete (status {v.get('status')}).")
        return reasons
    outcome = v.get("outcome")
    if outcome != FIX_DEMONSTRATED:
        reasons.append(f"The verification outcome is {outcome!r}, not a demonstrated fix.")

    # The evidence must belong to this exact patch on the inspected commit.
    if v.get("commit_sha") != candidate.commit_sha or candidate.commit_sha != commit_sha:
        reasons.append(
            "The verification's commit does not match the attempt's inspected commit."
        )
    if v.get("patch_sha256") != patch_fingerprint(candidate.diff):
        reasons.append("The verification's patch hash does not match this attempt's diff.")
    if v.get("image_id") != image_id:
        reasons.append("The verification ran on a different runner image than its siblings.")
    if v.get("profile") != profile:
        reasons.append("The verification used a different runner profile than its siblings.")
    if not v.get("patch_applied"):
        reasons.append("The patch was not applied.")

    comparison = v.get("comparison")
    if not isinstance(comparison, dict):
        reasons.append("No baseline-versus-patched comparison was recorded.")
    else:
        if not comparison.get("fixed"):
            reasons.append("No originally failing test was shown to pass.")
        for key, description in _MUST_BE_EMPTY:
            items = comparison.get(key) or []
            if items:
                reasons.append(f"{len(items)} {description}.")

    for label, key in (("baseline", "baseline_summary"), ("patched", "patched_summary")):
        summary = v.get(key)
        if not isinstance(summary, dict):
            reasons.append(f"No {label} run summary was recorded.")
            continue
        if summary.get("kind") not in _USABLE_RUN_KINDS:
            reasons.append(f"The {label} run ended as {summary.get('kind')!r}.")
        if summary.get("truncated"):
            reasons.append(f"The {label} report was truncated.")
        if summary.get("collect_errors"):
            reasons.append(f"The {label} run had collection errors.")

    return reasons


def evaluate(
    candidates: list[Candidate],
    *,
    commit_sha: str,
    image_id: str,
    profile: str,
    complete: bool,
) -> dict[str, Any]:
    """Judge every candidate and pick at most one, conservatively.

    `complete` is False when the orchestration was interrupted or could not
    finish every attempt; a recommendation is then explicitly scoped to the
    attempts that did finish.
    """
    rows: list[dict[str, Any]] = []
    eligible: list[tuple[Candidate, int]] = []
    for candidate in sorted(candidates, key=lambda c: c.attempt_index):
        reasons = _reasons(candidate, commit_sha=commit_sha, image_id=image_id, profile=profile)
        lines = changed_lines(candidate.diff)
        rows.append(
            {
                "attempt_index": candidate.attempt_index,
                "attempt_id": candidate.attempt_id,
                "eligible": not reasons,
                "reasons": reasons,
                "changed_lines": lines if candidate.diff else None,
                "outcome": (candidate.verification or {}).get("outcome"),
            }
        )
        if not reasons:
            eligible.append((candidate, lines))

    notes: list[str] = []
    completed = [
        c for c in candidates
        if c.verification is not None and c.verification.get("status") == "completed"
        and c.verification.get("baseline_summary")
    ]
    if len({_baseline_identity(c.verification) for c in completed}) > 1:  # type: ignore[arg-type]
        notes.append(
            "Completed verifications disagree about the baseline (collected tests or "
            "their original outcomes differ), although all ran the same commit. The "
            "suite may be nondeterministic."
        )

    baselines_consistent: bool | None = None
    recommended: Candidate | None = None
    if eligible:
        identities = {_baseline_identity(c.verification) for c, _ in eligible}  # type: ignore[arg-type]
        baselines_consistent = len(identities) == 1
        if baselines_consistent:
            recommended = min(eligible, key=lambda pair: (pair[1], pair[0].attempt_index))[0]

    if recommended is not None:
        scope = "all_attempts" if complete else "completed_attempts_only"
        headline = (
            f"Recommended: attempt {recommended.attempt_index}"
            if complete
            else f"Recommended among completed attempts only: attempt {recommended.attempt_index}"
        )
    elif eligible:
        scope = None
        headline = (
            "Demonstrated fixes found, but their baselines disagree — no cross-attempt "
            "recommendation"
        )
    else:
        scope = None
        headline = HEADLINE_NONE

    if not complete:
        notes.append(
            "The orchestration did not finish every attempt; this comparison covers only "
            "the attempts that did."
        )

    return {
        "rule": ELIGIBILITY_RULE,
        "tie_breaker": TIE_BREAKER,
        "complete": complete,
        "recommendation_scope": scope,
        "baselines_consistent": baselines_consistent,
        "recommended_attempt_index": recommended.attempt_index if recommended else None,
        "recommended_attempt_id": recommended.attempt_id if recommended else None,
        "headline": headline,
        "candidates": rows,
        "notes": notes,
    }
