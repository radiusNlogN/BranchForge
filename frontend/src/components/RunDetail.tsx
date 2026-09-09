import type { RunDetail as RunDetailData } from "../types";
import { formatAbsolute, formatRelative } from "../time";
import { Callout } from "./Callout";
import { InspectionPanel } from "./InspectionPanel";
import { NotImplementedNote } from "./NotImplementedNote";
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
        </div>
      ) : null}
    </section>
  );
}
