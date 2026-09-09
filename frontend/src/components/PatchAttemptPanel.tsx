import type { AttemptEvent, PatchAttempt, RunStatus } from "../types";
import { proposeCommand } from "../types";
import { formatAbsolute } from "../time";

/**
 * Renders one patch attempt.
 *
 * The diff and summary are model output and are rendered as plain text only.
 * There is no `dangerouslySetInnerHTML` here and no markdown or syntax
 * highlighting library that converts text to HTML. Diff lines are classified for
 * colour by inspecting their first character and emitting React elements — the
 * text itself is still escaped by React.
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
  runId: string;
  runStatus: RunStatus;
  attempt: PatchAttempt | null;
  refreshing: boolean;
  onRefresh: () => void;
}

export function PatchAttemptPanel({
  runId,
  runStatus,
  attempt,
  refreshing,
  onRefresh,
}: PatchAttemptPanelProps) {
  return (
    <section className="inspection">
      <div className="inspection__header">
        <h3>Proposed patch</h3>
        <div className="inspection__actions">
          {attempt ? (
            <span className={`badge badge--attempt-${attempt.status}`}>
              <span className="badge__dot" aria-hidden="true" />
              {attempt.status}
            </span>
          ) : null}
          <button
            type="button"
            className="button button--ghost"
            onClick={onRefresh}
            disabled={refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {attempt === null && runStatus !== "ready" ? (
        <div className="empty">
          <p className="empty__title">Inspect the repository first</p>
          <p className="empty__text">
            A patch can only be proposed for a run whose inspection has finished.
          </p>
        </div>
      ) : null}

      {attempt === null && runStatus === "ready" ? (
        <div className="empty">
          <p className="empty__title">No patch proposed yet</p>
          <p className="empty__text">
            Nothing runs automatically. Run the agent, then press Refresh:
          </p>
          <pre className="command">{proposeCommand(runId)}</pre>
          <p className="muted small">
            Run it from the <code>backend/</code> directory. It needs{" "}
            <code>ANTHROPIC_API_KEY</code> set.
          </p>
        </div>
      ) : null}

      {attempt?.status === "running" ? (
        <div className="empty">
          <p className="empty__title">Agent is working</p>
          <p className="empty__text">
            Press Refresh to see newly recorded activity. If the worker was interrupted, the
            attempt stays in this state — retries are not implemented.
          </p>
        </div>
      ) : null}

      {attempt?.status === "failed" ? (
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

      {attempt ? (
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
              {formatAbsolute(attempt.started_at)}
              {attempt.completed_at
                ? ` · finished ${formatAbsolute(attempt.completed_at)}`
                : ""}
            </dd>
          </div>
        </dl>
      ) : null}

      {attempt?.diff ? (
        <section className="reportSection">
          <h4>Patch</h4>
          {/* Adjacent to the diff, deliberately — not a footnote. */}
          <div className="unverified" role="note">
            <strong>Unverified patch — not applied or tested.</strong> This diff was generated by a
            model and has only been checked for valid unified-diff syntax. It has not been applied,
            compiled, or run against any test suite, and may not fix the issue.
          </div>
          <DiffView diff={attempt.diff} />
        </section>
      ) : null}

      {attempt?.summary ? (
        <section className="reportSection">
          <h4>Explanation</h4>
          <p className="attemptSummary">{attempt.summary}</p>
        </section>
      ) : null}

      {attempt?.suggested_test_command ? (
        <section className="reportSection">
          <h4>Suggested test command</h4>
          {/* Display only: BranchForge does not run this. */}
          <pre className="command">{attempt.suggested_test_command}</pre>
          <p className="muted small">
            Shown for reference. BranchForge does not run tests in this milestone.
          </p>
        </section>
      ) : null}

      {attempt ? (
        <section className="reportSection">
          <h4>Agent activity</h4>
          <EventList events={attempt.events} total={attempt.events_total} />
        </section>
      ) : null}
    </section>
  );
}
