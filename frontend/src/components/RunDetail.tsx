import type { PatchAttempt, RunDetail as RunDetailData } from "../types";
import { orchestrateCommand, proposeCommand, verifyAttemptCommand, verifyCommand } from "../types";
import { formatAbsolute, formatRelative } from "../time";
import { Callout } from "./Callout";
import { InspectionPanel } from "./InspectionPanel";
import { NotImplementedNote } from "./NotImplementedNote";
import { OrchestrationPanel } from "./OrchestrationPanel";
import { PatchAttemptPanel } from "./PatchAttemptPanel";
import { OUTCOME_LABELS, VerificationPanel } from "./VerificationPanel";
import { StatusBadge } from "./StatusBadge";

interface RunDetailProps {
  run: RunDetailData | null;
  loading: boolean;
  error: string | null;
  /** True when this run was created by the current session, for a confirmation line. */
  justCreated: boolean;
  onRetry: () => void;
  /** Re-fetch this run so a finished worker's report appears. */
  onRefresh: () => void;
}

function NoAttemptsYet({ run }: { run: RunDetailData }) {
  if (run.status !== "ready") {
    return (
      <div className="empty">
        <p className="empty__title">Inspect the repository first</p>
        <p className="empty__text">
          Patches can only be proposed for a run whose inspection has finished.
        </p>
      </div>
    );
  }
  return (
    <div className="empty">
      <p className="empty__title">No patch attempts yet</p>
      <p className="empty__text">
        Nothing runs automatically. Run {run.max_parallel_attempts} competing attempt
        {run.max_parallel_attempts === 1 ? "" : "s"}, each tested in a container and then compared:
      </p>
      <pre className="command">{orchestrateCommand(run.id)}</pre>
      <p className="empty__text">Or propose a single patch manually:</p>
      <pre className="command">{proposeCommand(run.id)}</pre>
      <p className="muted small">
        Run these from the <code>backend/</code> directory. They need <code>ANTHROPIC_API_KEY</code>;
        <code> orchestrate</code> also needs Docker and the runner image. Then press Refresh.
      </p>
    </div>
  );
}

function AttemptViews({
  run,
  attempt,
  loading,
  onRefresh,
}: {
  run: RunDetailData;
  attempt: PatchAttempt;
  loading: boolean;
  onRefresh: () => void;
}) {
  const orchestrationActive =
    run.orchestration !== null &&
    (run.orchestration.status === "queued" || run.orchestration.status === "running");
  const command =
    run.attempts.length === 1 && attempt.orchestration_id === null
      ? verifyCommand(run.id)
      : verifyAttemptCommand(attempt.id);
  return (
    <>
      <PatchAttemptPanel
        attempt={attempt}
        refreshing={loading}
        onRefresh={onRefresh}
        heading={run.attempts.length > 1 ? `Proposed patch — attempt ${attempt.attempt_index}` : "Proposed patch"}
      />
      <VerificationPanel
        command={command}
        hasAttempt={attempt.diff !== null}
        pendingByOrchestrator={orchestrationActive && attempt.pipeline_error_kind === null}
        verification={attempt.verification}
        refreshing={loading}
        onRefresh={onRefresh}
      />
    </>
  );
}

function attemptSummaryLine(attempt: PatchAttempt): string {
  const verification = attempt.verification;
  const evidence = verification?.outcome
    ? OUTCOME_LABELS[verification.outcome] ?? verification.outcome
    : verification
      ? `verification ${verification.status}`
      : attempt.pipeline_error_kind
        ? "verification never started"
        : "no verification";
  return `proposal ${attempt.status} · ${evidence}`;
}

export function RunDetail({
  run,
  loading,
  error,
  justCreated,
  onRetry,
  onRefresh,
}: RunDetailProps) {
  return (
    <section className="card card--detail">
      <div className="card__header">
        <h2>Run detail</h2>
        <p className="card__subtitle">Fetched from the backend, exactly as stored.</p>
      </div>

      {loading ? (
        <div className="detailBody" aria-busy="true">
          <span className="skeleton skeleton--title" />
          <span className="skeleton skeleton--meta" />
          <span className="skeleton skeleton--block" />
        </div>
      ) : null}

      {!loading && error ? (
        <Callout tone="error" title="Could not load this run" onRetry={onRetry}>
          <p>{error}</p>
        </Callout>
      ) : null}

      {!loading && !error && run === null ? (
        <div className="empty">
          <p className="empty__title">Nothing selected</p>
          <p className="empty__text">Choose a run from the list to inspect it.</p>
        </div>
      ) : null}

      {!loading && !error && run !== null ? (
        <div className="detailBody">
          {justCreated ? (
            <Callout tone="info" title="Run saved">
              <p>This is the record the backend persisted, read back from the database.</p>
            </Callout>
          ) : null}

          <div className="detailHead">
            <a
              className="detailHead__repo"
              href={run.repository_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {run.repository_url.replace(/^https:\/\/github\.com\//, "")}
            </a>
            <StatusBadge status={run.status} />
          </div>
          <NotImplementedNote compact />

          <dl className="properties">
            <div className="properties__row">
              <dt>Run ID</dt>
              <dd>
                <code>{run.id}</code>
              </dd>
            </div>
            <div className="properties__row">
              <dt>Repository</dt>
              <dd>
                <code>{run.repository_url}</code>
              </dd>
            </div>
            <div className="properties__row">
              <dt>Max parallel attempts</dt>
              <dd>{run.max_parallel_attempts}</dd>
            </div>
            <div className="properties__row">
              <dt>Created</dt>
              <dd>
                <time dateTime={run.created_at}>{formatAbsolute(run.created_at)}</time>{" "}
                <span className="muted">({formatRelative(run.created_at)})</span>
              </dd>
            </div>
            <div className="properties__row">
              <dt>Updated</dt>
              <dd>
                <time dateTime={run.updated_at}>{formatAbsolute(run.updated_at)}</time>
              </dd>
            </div>
          </dl>

          <div className="descriptionBlock">
            <h3>Issue description</h3>
            <p>{run.issue_description}</p>
          </div>

          <InspectionPanel
            runId={run.id}
            status={run.status}
            inspection={run.inspection}
            refreshing={loading}
            onRefresh={onRefresh}
          />

          {run.orchestration !== null ? (
            <OrchestrationPanel
              orchestration={run.orchestration}
              attempts={run.attempts}
              refreshing={loading}
              onRefresh={onRefresh}
            />
          ) : null}

          {run.attempts.length === 0 ? <NoAttemptsYet run={run} /> : null}

          {/* A manual run keeps its milestone-4 layout: one attempt, shown inline. */}
          {run.attempts.length === 1 && run.orchestration === null && run.attempts[0] ? (
            <AttemptViews
              run={run}
              attempt={run.attempts[0]}
              loading={loading}
              onRefresh={onRefresh}
            />
          ) : null}

          {run.orchestration !== null || run.attempts.length > 1
            ? run.attempts.map((attempt) => (
                <details
                  key={attempt.id}
                  className="attemptDetails"
                  open={run.orchestration?.recommended_attempt_index === attempt.attempt_index}
                >
                  <summary>
                    <strong>Attempt {attempt.attempt_index}</strong>{" "}
                    <span className="muted small">{attemptSummaryLine(attempt)}</span>
                  </summary>
                  <AttemptViews
                    run={run}
                    attempt={attempt}
                    loading={loading}
                    onRefresh={onRefresh}
                  />
                </details>
              ))
            : null}
        </div>
      ) : null}
    </section>
  );
}
