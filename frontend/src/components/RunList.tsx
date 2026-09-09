import type { Run } from "../types";
import { formatAbsolute, formatRelative } from "../time";
import { StatusBadge } from "./StatusBadge";

interface RunListProps {
  runs: Run[] | null;
  selectedId: string | null;
  onSelect: (runId: string) => void;
}

/** Strip the scheme so the list reads as `owner/repo`. */
function repoLabel(url: string): string {
  return url.replace(/^https:\/\/github\.com\//, "");
}

function Skeleton() {
  return (
    <ul className="runList" aria-busy="true" aria-label="Loading runs">
      {[0, 1, 2].map((index) => (
        <li key={index} className="runList__item runList__item--skeleton">
          <span className="skeleton skeleton--title" />
          <span className="skeleton skeleton--meta" />
        </li>
      ))}
    </ul>
  );
}

export function RunList({ runs, selectedId, onSelect }: RunListProps) {
  return (
    <section className="card">
      <div className="card__header">
        <h2>Recent runs</h2>
        <p className="card__subtitle">
          {runs === null ? "Loading…" : `${runs.length} newest first`}
        </p>
      </div>

      {runs === null ? <Skeleton /> : null}

      {runs !== null && runs.length === 0 ? (
        <div className="empty">
          <p className="empty__title">No runs yet</p>
          <p className="empty__text">Create a run and it will appear here.</p>
        </div>
      ) : null}

      {runs !== null && runs.length > 0 ? (
        <ul className="runList">
          {runs.map((run) => (
            <li key={run.id}>
              <button
                type="button"
                className={`runList__item${
                  run.id === selectedId ? " runList__item--selected" : ""
                }`}
                onClick={() => onSelect(run.id)}
                aria-current={run.id === selectedId}
              >
                <span className="runList__top">
                  <span className="runList__repo">{repoLabel(run.repository_url)}</span>
                  <StatusBadge status={run.status} />
                </span>
                <span className="runList__meta">
                  <time dateTime={run.created_at} title={formatAbsolute(run.created_at)}>
                    {formatRelative(run.created_at)}
                  </time>
                  <span className="runList__sep">·</span>
                  <span>
                    {run.max_parallel_attempts} attempt
                    {run.max_parallel_attempts === 1 ? "" : "s"}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
