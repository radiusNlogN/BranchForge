import { useCallback, useEffect, useState } from "react";

import { ApiError, API_BASE_URL, createRun, fetchRun, fetchRuns } from "./api";
import type { FieldErrors } from "./api";
import { Callout } from "./components/Callout";
import { NewRunForm } from "./components/NewRunForm";
import { NotImplementedNote } from "./components/NotImplementedNote";
import { RunDetail } from "./components/RunDetail";
import { RunList } from "./components/RunList";
import type { Run, RunCreateInput } from "./types";

const LIST_LIMIT = 25;

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "An unexpected error occurred.";
}

export default function App() {
  // `null` means "not loaded yet" and drives the loading state.
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<Run | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [createdId, setCreatedId] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    setListError(null);
    try {
      setRuns(await fetchRuns(LIST_LIMIT));
    } catch (error) {
      setRuns([]);
      setListError(messageOf(error));
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // The detail pane always reads the run back from the backend, so what is shown
  // is the persisted record rather than anything held locally.
  const loadDetail = useCallback(async (runId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      setSelectedRun(await fetchRun(runId));
    } catch (error) {
      setSelectedRun(null);
      setDetailError(messageOf(error));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      setSelectedRun(null);
      setDetailError(null);
      return;
    }
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // Once the user edits a field, the server's message about it is stale, so it
  // is cleared rather than left visible until the next submit.
  const dismissFieldError = useCallback((field: keyof RunCreateInput) => {
    setFieldErrors((previous) => {
      if (!(field in previous)) return previous;
      const next = { ...previous };
      delete next[field];
      return next;
    });
  }, []);

  const handleSubmit = useCallback(
    async (input: RunCreateInput): Promise<boolean> => {
      setSubmitting(true);
      setSubmitError(null);
      setFieldErrors({});
      try {
        const created = await createRun(input);
        setCreatedId(created.id);
        setSelectedId(created.id);
        await loadRuns();
        return true;
      } catch (error) {
        setSubmitError(messageOf(error));
        if (error instanceof ApiError) setFieldErrors(error.fieldErrors);
        return false;
      } finally {
        // Cleared in `finally` so a failed request never leaves the form stuck.
        setSubmitting(false);
      }
    },
    [loadRuns],
  );

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead__inner">
          <div className="brand">
            <span className="brand__mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <circle cx="6" cy="5" r="2.4" />
                <circle cx="18" cy="5" r="2.4" />
                <circle cx="12" cy="19" r="2.4" />
                <path d="M6 7.4v3.1a2.5 2.5 0 0 0 2.5 2.5h7A2.5 2.5 0 0 0 18 10.5V7.4" />
                <path d="M12 13v3.6" />
              </svg>
            </span>
            <div>
              <h1>
                Branch<span className="brand__accent">Forge</span>
              </h1>
              <p className="brand__tagline">
                Investigate GitHub issues with competing agent-generated fixes.
              </p>
            </div>
          </div>
          <span className="pill">milestone 1 · intake only</span>
        </div>
      </header>

      <main className="main">
        <NotImplementedNote />

        {listError ? (
          <Callout tone="error" title="Could not load runs" onRetry={() => void loadRuns()}>
            <p>{listError}</p>
            <p className="muted">
              API base URL: <code>{API_BASE_URL}</code>
            </p>
          </Callout>
        ) : null}

        <div className="layout">
          <div className="layout__column">
            <NewRunForm
              onSubmit={handleSubmit}
              submitting={submitting}
              serverFieldErrors={fieldErrors}
              onDismissFieldError={dismissFieldError}
            />
            {submitError ? (
              <Callout tone="error" title="Run was not created">
                <p>{submitError}</p>
              </Callout>
            ) : null}
            <RunList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
          </div>

          <div className="layout__column">
            <RunDetail
              run={selectedRun}
              loading={detailLoading}
              error={detailError}
              justCreated={selectedRun !== null && selectedRun.id === createdId}
              onRetry={() => {
                if (selectedId !== null) void loadDetail(selectedId);
              }}
            />
          </div>
        </div>
      </main>

      <footer className="footer">
        <span>BranchForge</span>
        <span className="muted">
          API <code>{API_BASE_URL}</code>
        </span>
      </footer>
    </div>
  );
}
