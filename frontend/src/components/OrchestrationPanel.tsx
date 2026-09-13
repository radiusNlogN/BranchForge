import type { Orchestration, OrchestrationComparison, PatchAttempt } from "../types";
import { formatAbsolute, formatDuration } from "../time";
import { LifecycleBadge } from "./StatusBadge";
import { OUTCOME_LABELS } from "./VerificationPanel";

/**
 * The competing-attempts summary.
 *
 * Two claims are kept visibly apart in every row: whether the *proposal* produced
 * a diff, and whether a *verification* demonstrated that originally failing tests
 * now pass. Only the second is evidence, and only an attempt meeting the backend's
 * conservative rule is ever recommended — otherwise the panel says "No
 * demonstrated fix" and recommends nothing. There is no pass/fail badge.
 *
 * All values are read from the persisted record; nothing here ticks or polls.
 */

const PROPOSAL_WORDS: Record<string, string> = {
  queued: "waiting for a slot",
  running: "in progress",
  succeeded: "diff produced",
  failed: "no diff",
  interrupted: "interrupted",
};

function evidenceCell(
  attempt: PatchAttempt,
  comparison: OrchestrationComparison | null,
  recommended: number | null,
) {
  const row = comparison?.candidates.find((c) => c.attempt_index === attempt.attempt_index);
  if (!row) {
    return <span className="muted">not compared yet</span>;
  }
  return (
    <div>
      <strong>
        {row.eligible ? "Demonstrated fix" : "No demonstrated fix"}
        {recommended === attempt.attempt_index ? " · recommended" : ""}
      </strong>
      {row.reasons.length > 0 ? (
        <details className="reasons">
          <summary>Why not eligible ({row.reasons.length})</summary>
          <ul>
            {row.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function ComparisonSummary({ orchestration }: { orchestration: Orchestration }) {
  const comparison = orchestration.comparison;
  if (comparison === null) {
    return (
      <p className="muted small">
        The comparison is written once every attempt has finished.
      </p>
    );
  }
  const recommended = comparison.recommended_attempt_index;
  return (
    <>
      <div
        className={`verify__outcome verify__outcome--${recommended !== null ? "good" : "neutral"}`}
        role="note"
      >
        <strong>{comparison.headline}</strong>
        {recommended !== null ? (
          <p>
            Attempt {recommended}&apos;s verification showed every originally failing original test
            now passing, with no regressions, missing tests, truncated reports, or collection
            problems, on this attempt&apos;s exact patch. {comparison.tie_breaker}
          </p>
        ) : (
          <p>
            No attempt met the bar: {comparison.rule} Nothing is recommended — the least-bad patch is
            not promoted.
          </p>
        )}
      </div>
      {!comparison.complete ? (
        <div className="callout callout--warn" role="note">
          <strong>Not every attempt finished.</strong>
          <p>
            {recommended !== null
              ? "This is a recommendation among the attempts that completed only — not a completed comparison."
              : "The comparison covers only the attempts that completed."}
          </p>
        </div>
      ) : null}
      {comparison.baselines_consistent === false ? (
        <div className="callout callout--warn" role="note">
          <strong>Baselines disagree.</strong>
          <p>
            The attempts&apos; baseline runs of the same commit did not match, so no cross-attempt
            recommendation is made.
          </p>
        </div>
      ) : null}
      <p className="verify__scope small">
        Based only on the repository&apos;s original tests that ran at commit{" "}
        <code>{orchestration.commit_sha.slice(0, 12)}</code> in the{" "}
        <code>{orchestration.profile}</code> profile. Tests a patch adds are never counted. This
        is not a proof of correctness.
      </p>
    </>
  );
}

interface Props {
  orchestration: Orchestration;
  attempts: PatchAttempt[];
  /**
   * True when the run's job was cancelled. `interrupted` is all an orchestrator
   * can record about being stopped — it cannot know *why* — so without this the
   * panel would report "interrupted" beside a job that plainly says "cancelled",
   * and the two would look like they disagreed about the same event.
   */
  cancelRequested: boolean;
}

export function OrchestrationPanel({ orchestration, attempts, cancelRequested }: Props) {
  const comparison = orchestration.comparison;
  const finished = orchestration.completed_at !== null;
  return (
    <section className="inspection">
      <div className="inspection__header">
        <h3>Competing attempts</h3>
        <div className="inspection__actions">
          <LifecycleBadge family="orch" status={orchestration.status} />
        </div>
      </div>

      {orchestration.status === "queued" || orchestration.status === "running" ? (
        <p className="muted small">
          The orchestrator process is working. While a job is running this page updates itself;
          Refresh still works. If that process was killed outright, this state can remain —
          automatic recovery is not implemented.
        </p>
      ) : null}
      {orchestration.status === "interrupted" ? (
        <div className="callout callout--warn" role="note">
          <strong>{cancelRequested ? "Stopped by cancellation." : "Interrupted."}</strong>
          <p>
            {orchestration.error_message} Attempts that had not finished are marked interrupted,
            and their containers were removed.
          </p>
        </div>
      ) : null}
      {orchestration.status === "failed" ? (
        <div className="callout callout--error" role="alert">
          <strong>The orchestration itself failed ({orchestration.error_kind}).</strong>
          <p>{orchestration.error_message}</p>
        </div>
      ) : null}

      <dl className="properties">
        <div className="properties__row">
          <dt>Attempts</dt>
          <dd>{orchestration.requested_attempts} requested</dd>
        </div>
        <div className="properties__row">
          <dt>Concurrency</dt>
          <dd>
            at most {orchestration.effective_concurrency} at once{" "}
            <span className="muted">
              (limit {orchestration.concurrency_limit} per orchestrator process — not a global
              limit)
            </span>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Model</dt>
          <dd>
            <code>{orchestration.model}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Commit</dt>
          <dd>
            <code>{orchestration.commit_sha}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Runner</dt>
          <dd>
            <code>{orchestration.profile}</code> · <code>{orchestration.image_id.slice(0, 19)}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Time</dt>
          <dd>
            {orchestration.started_at ? formatAbsolute(orchestration.started_at) : "not started"}
            {finished && orchestration.completed_at
              ? ` · finished ${formatAbsolute(orchestration.completed_at)}`
              : ""}{" "}
            <span className="muted">
              ({formatDuration(orchestration.started_at, orchestration.completed_at)}
              {finished ? "" : " so far, as of last refresh"})
            </span>
          </dd>
        </div>
      </dl>

      <ComparisonSummary orchestration={orchestration} />

      <div className="tableWrap">
        <table className="attemptTable">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Emphasis</th>
              <th scope="col">Proposal</th>
              <th scope="col">Verification</th>
              <th scope="col">Outcome</th>
              <th scope="col">Test evidence</th>
              <th scope="col">Tokens</th>
              <th scope="col">Elapsed</th>
            </tr>
          </thead>
          <tbody>
            {attempts.map((attempt) => {
              const verification = attempt.verification;
              return (
                <tr key={attempt.id}>
                  <td>{attempt.attempt_index}</td>
                  <td>
                    <code className="small">{attempt.emphasis_key ?? "—"}</code>
                  </td>
                  <td>
                    <LifecycleBadge
                      family="attempt"
                      status={attempt.status}
                      label={PROPOSAL_WORDS[attempt.status] ?? attempt.status}
                    />
                  </td>
                  <td>
                    {verification ? (
                      <LifecycleBadge family="verify" status={verification.status} />
                    ) : attempt.pipeline_error_kind ? (
                      <span className="warn small">never started</span>
                    ) : (
                      <span className="muted small">none</span>
                    )}
                  </td>
                  <td className="small">
                    {verification?.outcome
                      ? OUTCOME_LABELS[verification.outcome] ?? verification.outcome
                      : "—"}
                  </td>
                  <td className="small">
                    {evidenceCell(attempt, comparison, orchestration.recommended_attempt_index)}
                  </td>
                  <td className="small">
                    {attempt.input_tokens === null && attempt.output_tokens === null
                      ? <span className="muted">not reported</span>
                      : `in ${attempt.input_tokens ?? "—"} · out ${attempt.output_tokens ?? "—"}`}
                  </td>
                  <td className="small">
                    {formatDuration(attempt.started_at, attempt.completed_at)}
                    {attempt.started_at !== null && attempt.completed_at === null ? " so far" : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {orchestration.notes && orchestration.notes.length > 0 ? (
        <ul className="small muted">
          {orchestration.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      {comparison?.notes && comparison.notes.length > 0 ? (
        <ul className="small muted">
          {comparison.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      <p className="muted small">
        Each attempt had the same issue, inspection report, and commit, and differed only in the
        recorded investigation emphasis. That does not guarantee the attempts reached different
        fixes. Select an attempt below for its diff, activity, and test output.
      </p>
    </section>
  );
}
