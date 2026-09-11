import type { ComparisonData, RunSummaryData, Verification } from "../types";
import { formatAbsolute } from "../time";

/**
 * Renders one verification.
 *
 * The deliberate design choice here is that there is **no pass/fail badge**. The
 * outcome is stated in words, taken from a fixed vocabulary the backend owns, so
 * the UI cannot quietly promote "the tests we ran changed behaviour" into
 * "verified fix". `status` (did it run) and `outcome` (what it found) are shown
 * separately for the same reason.
 *
 * Container logs are untrusted program output: they render as plain text inside
 * `<pre>`, with no `dangerouslySetInnerHTML` and no highlighting library.
 */

/**
 * Wording for each outcome. Neutral phrasing; nothing here says "verified".
 * Exported so the orchestration summary uses the same words.
 */
export const OUTCOME_LABELS: Record<string, string> = {
  fix_demonstrated: "Originally failing tests now pass",
  partial_fix: "Some originally failing tests now pass",
  still_failing: "Originally failing tests still fail",
  regressions: "The patch broke previously passing tests",
  no_bug_demonstrated: "Existing tests pass; bug fix not demonstrated",
  tests_only_patch: "The patch changes only tests or configuration",
  patch_did_not_apply: "The patch does not apply to this commit",
  collection_mismatch: "The two runs collected different tests — not comparable",
  patched_collection_error: "The patched source fails to import or collect",
  baseline_unusable: "The baseline run produced no usable result",
  unsupported_layout: "No original pytest suite was found",
  environment_limitation: "The runner profile cannot build this repository",
  timeout: "A test run exceeded its time limit",
  inconclusive: "The runs could not be compared reliably",
};

/** Which outcomes read as good news. Only two do. */
const POSITIVE = new Set(["fix_demonstrated", "partial_fix"]);
const NEUTRAL = new Set([
  "no_bug_demonstrated",
  "tests_only_patch",
  "collection_mismatch",
  "unsupported_layout",
  "environment_limitation",
  "inconclusive",
  "baseline_unusable",
]);

function outcomeTone(outcome: string | null): string {
  if (!outcome) return "neutral";
  if (POSITIVE.has(outcome)) return "good";
  if (NEUTRAL.has(outcome)) return "neutral";
  return "bad";
}

/** Human wording for a run classification. */
const RUN_KIND_LABELS: Record<string, string> = {
  ok: "all tests passed",
  tests_failed: "tests failed",
  interrupted: "run interrupted",
  internal_error: "pytest internal error",
  usage_error: "pytest usage error",
  no_tests_collected: "no tests collected",
  collection_error: "collection or import error",
  timeout: "timed out",
  no_report: "no structured report produced",
};

function LogBlock({ title, log }: { title: string; log: string }) {
  return (
    <details className="log">
      <summary>{title}</summary>
      <pre className="log__body">{log}</pre>
    </details>
  );
}

function RunColumn({ label, summary }: { label: string; summary: RunSummaryData | null }) {
  if (summary === null) {
    return (
      <div className="verify__run">
        <h4>{label}</h4>
        <p className="muted small">Not run.</p>
      </div>
    );
  }
  const counts = summary.counts ?? {};
  const order = ["passed", "failed", "error", "skipped", "xfailed", "xpassed"];
  return (
    <div className="verify__run">
      <h4>{label}</h4>
      <p className="small">
        <strong>{RUN_KIND_LABELS[summary.kind] ?? summary.kind}</strong>
        {summary.exit_code !== null ? ` (exit ${summary.exit_code})` : ""}
      </p>
      <dl className="verify__counts">
        {order
          .filter((key) => (counts[key] ?? 0) > 0)
          .map((key) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{counts[key]}</dd>
            </div>
          ))}
      </dl>
      <p className="muted small">
        {summary.collected.length} test(s) collected in {summary.duration_seconds.toFixed(1)}s
      </p>
      {summary.report_error !== null && (
        <p className="small warn">{summary.report_error}</p>
      )}
      {summary.truncated && (
        <p className="small warn">
          This report was truncated — the suite exceeded the reporting cap, so only part of
          it is recorded.
        </p>
      )}
      {summary.collect_errors.length > 0 && (
        <ul className="small warn">
          {summary.collect_errors.slice(0, 5).map((problem) => (
            <li key={problem.nodeid}>
              <code>{problem.nodeid}</code>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TestIdList({ title, ids }: { title: string; ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <div className="verify__ids">
      <h5>
        {title} <span className="muted">({ids.length})</span>
      </h5>
      <ul>
        {ids.slice(0, 25).map((id) => (
          <li key={id}>
            <code>{id}</code>
          </li>
        ))}
      </ul>
      {ids.length > 25 && <p className="muted small">…and {ids.length - 25} more.</p>}
    </div>
  );
}

function ComparisonDetail({ comparison }: { comparison: ComparisonData }) {
  return (
    <>
      <TestIdList title="Now passing (were failing)" ids={comparison.fixed} />
      <TestIdList title="Still failing" ids={comparison.still_failing} />
      <TestIdList title="Newly failing (regressions)" ids={comparison.regressions} />
      <TestIdList
        title="No longer run — not counted as fixed"
        ids={comparison.no_longer_exercised}
      />
      <TestIdList title="Previously passing, no longer run" ids={comparison.weakened} />
      <TestIdList title="Missing from the patched run" ids={comparison.missing_from_patched} />
      <TestIdList title="Added in the patched run" ids={comparison.added_in_patched} />
    </>
  );
}

interface Props {
  /** The exact command that verifies THIS attempt. */
  command: string;
  hasAttempt: boolean;
  /** An active orchestrator will verify this patch itself. */
  pendingByOrchestrator: boolean;
  verification: Verification | null;
  refreshing: boolean;
  onRefresh: () => void;
}

export function VerificationPanel({
  command,
  hasAttempt,
  pendingByOrchestrator,
  verification,
  refreshing,
  onRefresh,
}: Props) {
  if (!hasAttempt) {
    return null;
  }

  if (verification === null) {
    return (
      <section className="panel">
        <h3>Verification</h3>
        {pendingByOrchestrator ? (
          <p className="muted">
            The orchestrator verifies this patch as the next step of its pipeline. Nothing polls
            automatically — press Refresh.
          </p>
        ) : (
          <>
            <p className="muted">
              The proposed patch has not been applied or tested. To run the repository's own
              test suite before and after the patch, in a container with no network:
            </p>
            <pre className="command">{command}</pre>
            <p className="muted small">
              Requires Docker and the runner image. Refresh once it finishes.
            </p>
          </>
        )}
        <button type="button" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </section>
    );
  }

  const tone = outcomeTone(verification.outcome);

  return (
    <section className="panel">
      <div className="panel__head">
        <h3>Verification</h3>
        <span className={`badge badge--verify-${verification.status}`}>
          {verification.status}
        </span>
        <button type="button" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {verification.status === "running" && (
        <p className="muted">
          Containers are running. Nothing polls automatically — use Refresh.
        </p>
      )}

      {verification.status === "failed" && (
        <div className="callout callout--error" role="alert">
          <strong>The verification could not run to completion.</strong>
          <p>{verification.error_message}</p>
          {verification.error_kind !== null && (
            <p className="small muted">Reason: {verification.error_kind}</p>
          )}
        </div>
      )}

      {verification.status === "interrupted" && (
        <div className="callout callout--warn" role="note">
          <strong>The verification was interrupted.</strong>
          <p>{verification.error_message}</p>
          <p className="small muted">
            Its containers were removed. No result was recorded and none is implied.
          </p>
        </div>
      )}

      {verification.outcome !== null && (
        <div className={`verify__outcome verify__outcome--${tone}`} role="note">
          <strong>
            {OUTCOME_LABELS[verification.outcome] ?? verification.outcome}
          </strong>
          {verification.detail !== null && <p>{verification.detail}</p>}
        </div>
      )}

      {/* The scope of the claim, stated rather than implied. */}
      <p className="verify__scope small">
        This is the result of running the repository's own tests at commit{" "}
        <code>{(verification.commit_sha ?? "").slice(0, 12)}</code> in the{" "}
        <code>{verification.profile}</code> profile. It covers only the tests that
        actually ran. It is not a proof of correctness, and no dependencies from the
        repository were installed.
      </p>

      <h4>Patch application</h4>
      <p className="small">
        {verification.patch_applied ? "Applied cleanly." : "Did not apply."}{" "}
        {verification.patch_apply_message}
      </p>
      {verification.files_changed !== null && verification.files_changed.length > 0 && (
        <ul className="small">
          {verification.files_changed.map((path) => (
            <li key={path}>
              <code>{path}</code>
            </li>
          ))}
        </ul>
      )}

      {verification.patch_touched_tests && (
        <div className="callout callout--warn" role="note">
          <strong>This patch modified tests or test configuration.</strong>
          <p>
            The original test files and collection configuration were restored for the
            comparison run, so the patch could not change the tests it is measured
            against. Any tests the patch adds were run separately below.
          </p>
        </div>
      )}

      <div className="verify__runs">
        <RunColumn label="Baseline (original source)" summary={verification.baseline_summary} />
        <RunColumn
          label="Patched (original tests)"
          summary={verification.patched_summary}
        />
      </div>

      {verification.comparison !== null && (
        <ComparisonDetail comparison={verification.comparison} />
      )}

      {verification.supplemental_summary !== null && (
        <>
          <h4>Supplemental: the patch's own tests</h4>
          <p className="muted small">
            Separate evidence, run against the fully patched tree including the patch's
            own test changes. It does not affect the comparison above.
          </p>
          <RunColumn
            label="Patch's own tests"
            summary={verification.supplemental_summary}
          />
        </>
      )}

      {verification.notes !== null && verification.notes.length > 0 && (
        <ul className="small muted">
          {verification.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}

      {verification.baseline_log && (
        <LogBlock title="Baseline test output" log={verification.baseline_log} />
      )}
      {verification.patched_log && (
        <LogBlock title="Patched test output" log={verification.patched_log} />
      )}
      {verification.supplemental_log && (
        <LogBlock title="Supplemental test output" log={verification.supplemental_log} />
      )}

      {verification.runner_args !== null && (
        <details className="log">
          <summary>Exact runner command</summary>
          <pre className="log__body">{verification.runner_args.join(" ")}</pre>
        </details>
      )}

      <p className="muted small">
        Started {formatAbsolute(verification.started_at)}
        {verification.completed_at !== null
          ? ` · finished ${formatAbsolute(verification.completed_at)}`
          : ""}
      </p>
    </section>
  );
}
