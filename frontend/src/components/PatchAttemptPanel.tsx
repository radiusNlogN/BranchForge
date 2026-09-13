import type { AttemptEvent, PatchAttempt } from "../types";
import { formatAbsolute, formatDuration } from "../time";
import { LifecycleBadge } from "./StatusBadge";

/**
 * Renders one patch attempt.
 *
 * The diff and summary are model output and are rendered as plain text only.
 * There is no `dangerouslySetInnerHTML` here and no markdown or syntax
 * highlighting library that converts text to HTML. Diff lines are classified for
 * colour by inspecting their first character and emitting React elements — the
 * text itself is still escaped by React.
 *
 * `status` here describes the *proposal* only. "succeeded" means a diff was
 * produced, never that it works; what the tests showed lives in the verification.
 *
 * The suggested test command is display-only. Nothing in this panel runs
 * anything.
 */

function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff">
      {lines.map((line, index) => {
        let kind = "ctx";
        if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "del";
        return (
          <span key={index} className={`diff__line diff__line--${kind}`}>
            {line === "" ? " " : line}
          </span>
        );
      })}
    </pre>
  );
}

function EventList({ events, total }: { events: AttemptEvent[]; total: number }) {
  if (events.length === 0) {
    return <p className="muted small">No activity recorded yet.</p>;
  }
  return (
    <>
      <ol className="events">
        {events.map((event) => (
          <li key={event.seq}>
            <span className={`events__kind events__kind--${event.kind}`}>{event.kind}</span>
            <span className="events__summary">{event.summary}</span>
            {event.detail ? <span className="events__detail">{event.detail}</span> : null}
          </li>
        ))}
      </ol>
      {total > events.length ? (
        <p className="muted small">
          Showing {events.length} of {total} events.
        </p>
      ) : null}
    </>
  );
}

interface PatchAttemptPanelProps {
  attempt: PatchAttempt;
  heading: string;
}

/**
 * Refresh lives once, in the run page header. This panel used to carry its own
 * copy — as did the inspection, orchestration, and verification panels — four
 * buttons calling one handler with one disabled flag.
 */
export function PatchAttemptPanel({ attempt, heading }: PatchAttemptPanelProps) {
  const hasVerification = attempt.verification !== null;
  return (
    <section className="inspection">
      <div className="inspection__header">
        <h3>{heading}</h3>
        <div className="inspection__actions">
          <LifecycleBadge family="attempt" status={attempt.status} label={`proposal ${attempt.status}`} />
        </div>
      </div>

      {attempt.status === "queued" ? (
        <div className="empty">
          <p className="empty__title">Reserved, waiting for a slot</p>
          <p className="empty__text">
            The orchestrator starts this attempt when a concurrency slot frees up.
          </p>
        </div>
      ) : null}

      {attempt.status === "running" ? (
        <div className="empty">
          <p className="empty__title">Agent is working</p>
          <p className="empty__text">
            Newly recorded activity appears as the job progresses. If the worker process was
            killed outright, the attempt can stay in this state — automatic recovery and retries
            are not implemented.
          </p>
        </div>
      ) : null}

      {attempt.status === "failed" ? (
        <div className="callout callout--error" role="alert">
          <div className="callout__body">
            <p className="callout__title">
              No patch was produced{attempt.error_kind ? ` (${attempt.error_kind})` : ""}
            </p>
            <div className="callout__text">
              <p>{attempt.error_message}</p>
              <p className="muted">
                The repository inspection is unaffected — this run is still <code>ready</code>.
              </p>
            </div>
          </div>
        </div>
      ) : null}

      {attempt.status === "interrupted" ? (
        <div className="callout callout--warn" role="note">
          <strong>Interrupted before a patch was produced.</strong>
          <p>{attempt.error_message}</p>
        </div>
      ) : null}

      {attempt.status === "succeeded" && attempt.pipeline_error_kind !== null ? (
        <div className="callout callout--warn" role="note">
          <strong>
            A patch was produced, but its pipeline did not finish ({attempt.pipeline_error_kind}).
          </strong>
          <p>{attempt.pipeline_error_message}</p>
          <p>No test improvement was demonstrated for this patch.</p>
        </div>
      ) : null}

      <dl className="properties">
        <div className="properties__row">
          <dt>Model</dt>
          <dd>
            <code>{attempt.model}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Against commit</dt>
          <dd>
            <code>{attempt.commit_sha ?? "—"}</code>
          </dd>
        </div>
        {attempt.emphasis_text !== null ? (
          <div className="properties__row">
            <dt>Investigation emphasis</dt>
            <dd>
              <code>{attempt.emphasis_key}</code> — {attempt.emphasis_text}
            </dd>
          </div>
        ) : null}
        <div className="properties__row">
          <dt>Tokens</dt>
          <dd>
            {attempt.input_tokens === null && attempt.output_tokens === null ? (
              <span className="muted">not reported</span>
            ) : (
              <>
                in {attempt.input_tokens ?? "—"} · out {attempt.output_tokens ?? "—"}
              </>
            )}
          </dd>
        </div>
        <div className="properties__row">
          <dt>Started</dt>
          <dd>
            {attempt.started_at ? formatAbsolute(attempt.started_at) : "not started"}
            {attempt.completed_at ? ` · finished ${formatAbsolute(attempt.completed_at)}` : ""}
            {attempt.started_at ? (
              <span className="muted">
                {" "}
                ({formatDuration(attempt.started_at, attempt.completed_at)}
                {attempt.completed_at === null ? " so far, as of last refresh" : ""})
              </span>
            ) : null}
          </dd>
        </div>
      </dl>

      {attempt.diff ? (
        <section className="reportSection">
          <h4>Patch</h4>
          {/* Adjacent to the diff, deliberately — not a footnote. */}
          {hasVerification ? (
            <div className="unverified" role="note">
              <strong>Model-generated patch — not proven correct.</strong> It was applied only to a
              throwaway copy of the inspected commit to run the repository&apos;s tests; the result is
              in the verification below and covers only the tests that ran. It has not been applied to
              any checkout of yours.
            </div>
          ) : (
            <div className="unverified" role="note">
              <strong>Unverified patch — not applied or tested.</strong> This diff was generated by a
              model and has only been checked for valid unified-diff syntax. It has not been applied,
              compiled, or run against any test suite, and may not fix the issue.
            </div>
          )}
          <DiffView diff={attempt.diff} />
        </section>
      ) : null}

      {attempt.summary ? (
        <section className="reportSection">
          <h4>Explanation</h4>
          <p className="attemptSummary">{attempt.summary}</p>
        </section>
      ) : null}

      {attempt.suggested_test_command ? (
        <section className="reportSection">
          <h4>Suggested test command</h4>
          {/* Display only: BranchForge does not run this. */}
          <pre className="command">{attempt.suggested_test_command}</pre>
          <p className="muted small">
            Shown for reference. BranchForge never runs this command; verification uses its own
            fixed pytest invocation.
          </p>
        </section>
      ) : null}

      <section className="reportSection">
        <h4>Agent activity</h4>
        <EventList events={attempt.events} total={attempt.events_total} />
      </section>
    </section>
  );
}
