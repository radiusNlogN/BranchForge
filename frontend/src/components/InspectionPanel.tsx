import type { FilePreview, Inspection, InspectionReport, RunStatus } from "../types";
import { workerCommand } from "../types";
import { formatAbsolute } from "../time";

/**
 * Renders a persisted inspection report.
 *
 * Everything here originates in a third-party repository and is untrusted. It is
 * rendered as plain text only — React escapes by default and this file uses no
 * `dangerouslySetInnerHTML` and no markdown-to-HTML conversion. Keep it that way.
 */

function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function Verdict({ value, label }: { value: boolean; label: string }) {
  return (
    <span className={`verdict verdict--${value ? "yes" : "no"}`}>
      <span aria-hidden="true">{value ? "✓" : "✗"}</span> {label}
    </span>
  );
}

function EvidenceList({ items }: { items: string[] }) {
  if (items.length === 0) return <p className="muted small">No supporting files found.</p>;
  return (
    <ul className="evidence">
      {items.map((item) => (
        <li key={item}>
          <code>{item}</code>
        </li>
      ))}
    </ul>
  );
}

function Preview({ preview }: { preview: FilePreview }) {
  if (preview.omitted) {
    return (
      <div className="preview preview--omitted">
        <div className="preview__head">
          <code>{preview.path}</code>
          <span className="muted small">{formatBytes(preview.size)}</span>
        </div>
        <p className="muted small">{preview.omitted_reason ?? "Not stored."}</p>
      </div>
    );
  }

  return (
    <details className="preview">
      <summary>
        <code>{preview.path}</code>
        <span className="muted small">
          {formatBytes(preview.size)}
          {preview.content_truncated ? " · truncated" : ""}
        </span>
      </summary>
      {/* Plain text. Never rendered as HTML or markdown. */}
      <pre className="preview__body">{preview.content}</pre>
      {preview.content_truncated ? (
        <p className="muted small">Truncated to fit the total content budget.</p>
      ) : null}
    </details>
  );
}

function Report({ report }: { report: InspectionReport }) {
  const { repository, assessment, budgets, truncation } = report;

  return (
    <div className="report">
      {repository.description ? (
        <p className="report__description">{repository.description}</p>
      ) : (
        <p className="muted small">This repository has no description.</p>
      )}

      <dl className="properties">
        <div className="properties__row">
          <dt>Inspected commit</dt>
          <dd>
            <code>{repository.commit_sha ?? "—"}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Default branch</dt>
          <dd>
            <code>{repository.default_branch ?? "—"}</code>
          </dd>
        </div>
        <div className="properties__row">
          <dt>Files in tree</dt>
          <dd>
            {budgets.files_in_tree.toLocaleString()}
            {truncation.file_listing_truncated
              ? ` (showing ${budgets.files_listed.toLocaleString()})`
              : ""}
          </dd>
        </div>
        <div className="properties__row">
          <dt>GitHub requests used</dt>
          <dd>
            {budgets.requests_made} of {budgets.max_requests}
          </dd>
        </div>
      </dl>

      <section className="reportSection">
        <h4>Python / pytest assessment</h4>
        <div className="verdicts">
          <Verdict value={assessment.is_python_project} label="Python project" />
          <Verdict value={assessment.uses_pytest} label="Uses pytest" />
        </div>
        <h5>Supporting files — Python</h5>
        <EvidenceList items={assessment.python_evidence} />
        <h5>Supporting files — pytest</h5>
        <EvidenceList items={assessment.pytest_evidence} />
        {assessment.test_locations.length > 0 ? (
          <>
            <h5>Likely test locations ({assessment.test_locations.length})</h5>
            <ul className="evidence evidence--scroll">
              {assessment.test_locations.map((path) => (
                <li key={path}>
                  <code>{path}</code>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <h5>Likely test locations</h5>
            <p className="muted small">No test files or directories matched.</p>
          </>
        )}
        <p className="note note--compact">{assessment.caveat}</p>
      </section>

      {truncation.notes.length > 0 || truncation.omitted_files.length > 0 ? (
        <section className="reportSection">
          <h4>Omissions and truncation</h4>
          {truncation.notes.map((note) => (
            <p key={note} className="muted small">
              {note}
            </p>
          ))}
          {truncation.omitted_files.length > 0 ? (
            <ul className="evidence">
              {truncation.omitted_files.map((entry) => (
                <li key={entry} className="small">
                  {entry}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      <section className="reportSection">
        <h4>Source previews</h4>
        {report.readme ? (
          <Preview preview={report.readme} />
        ) : (
          <p className="muted small">No README found in this repository.</p>
        )}
        {report.python_config_files.length > 0 ? (
          report.python_config_files.map((preview) => (
            <Preview key={preview.path} preview={preview} />
          ))
        ) : (
          <p className="muted small">No Python configuration files found.</p>
        )}
      </section>

      <section className="reportSection">
        <h4>
          Files <span className="muted small">({budgets.files_listed.toLocaleString()} listed)</span>
        </h4>
        <ul className="fileList">
          {report.files.map((file) => (
            <li key={file.path}>
              <code>{file.path}</code>
              <span className="muted small">{formatBytes(file.size)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

interface InspectionPanelProps {
  runId: string;
  status: RunStatus;
  inspection: Inspection | null;
  /** `false` only when the backend says this deployment runs no dispatcher. */
  dispatcherAvailable?: boolean | null;
}

/** Refresh lives once, in the run page header — not in each panel. */
export function InspectionPanel({
  runId,
  status,
  inspection,
  dispatcherAvailable = null,
}: InspectionPanelProps) {
  return (
    <section className="inspection">
      <div className="inspection__header">
        <h3>Repository inspection</h3>
      </div>

      {status === "pending" && dispatcherAvailable === false ? (
        <div className="empty">
          <p className="empty__title">Not inspected</p>
          <p className="empty__text">
            Nothing on this deployment inspects repositories, so this run stays uninspected here.
          </p>
        </div>
      ) : null}

      {status === "pending" && dispatcherAvailable !== false ? (
        <div className="empty">
          <p className="empty__title">Not inspected yet</p>
          <p className="empty__text">
            Starting this run inspects the repository first. To do it by hand instead:
          </p>
          <pre className="command">{workerCommand(runId)}</pre>
          <p className="muted small">Run it from the <code>backend/</code> directory.</p>
        </div>
      ) : null}

      {status === "inspecting" ? (
        <div className="empty">
          <p className="empty__title">Inspection in progress</p>
          <p className="empty__text">
            A worker has claimed this run. Press Refresh to check whether it has finished.
          </p>
          <p className="muted small">
            If the worker was interrupted, the run stays in this state — this milestone has no
            recovery for stuck runs.
          </p>
        </div>
      ) : null}

      {status === "failed" && inspection ? (
        <div className="callout callout--error" role="alert">
          <div className="callout__body">
            <p className="callout__title">
              Inspection failed{inspection.error_kind ? ` (${inspection.error_kind})` : ""}
            </p>
            <div className="callout__text">
              <p>{inspection.error_message}</p>
            </div>
          </div>
        </div>
      ) : null}

      {inspection ? (
        <p className="muted small">
          Started {formatAbsolute(inspection.started_at)}
          {inspection.completed_at ? ` · finished ${formatAbsolute(inspection.completed_at)}` : ""}
        </p>
      ) : null}

      {inspection?.report ? <Report report={inspection.report} /> : null}
    </section>
  );
}
