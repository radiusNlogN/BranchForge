import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  API_BASE_URL,
  cancelRun,
  createRun,
  fetchRun,
  fetchRunProgress,
  fetchRuns,
  startRun,
} from "./api";
import type { FieldErrors } from "./api";
import { HomePage } from "./components/HomePage";
import { RunPage } from "./components/RunPage";
import { hasActiveJob, mergeProgress, progressSignature } from "./progress";
import { HOME_HREF, hrefForRun, navigate, useHashRoute } from "./routing";
import type { Run, RunCreateInput, RunDetail as RunDetailData, RunProgress } from "./types";

const LIST_LIMIT = 25;
const POLL_INTERVAL_MS = 2000;

function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "An unexpected error occurred.";
}

export default function App() {
  const route = useHashRoute();
  // The route is the single source of truth for which run is open. Deriving
  // `selectedId` from it — rather than letting navigation merely swap which
  // component renders — is what keeps the existing polling teardown correct:
  // leaving a run sets this to null, which the effect below already handles.
  const selectedId = route.name === "run" ? route.runId : null;

  // `null` means "not loaded yet" and drives the loading state.
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedRun, setSelectedRun] = useState<RunDetailData | null>(null);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [createdId, setCreatedId] = useState<string | null>(null);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Every asynchronous result is checked against these before it is allowed to
  // touch state. Responses do not arrive in the order they were sent, and the
  // user can navigate mid-flight, so a late answer about the previous run would
  // otherwise overwrite the current one with someone else's data.
  const selectedIdRef = useRef<string | null>(null);
  const detailTokenRef = useRef(0);
  const progressTokenRef = useRef(0);
  const progressInFlightRef = useRef(false);
  const actionTokenRef = useRef(0);
  const signatureRef = useRef("");
  // Read inside `pollOnce` so a tick knows whether the list is even on screen.
  const onHomeRef = useRef(route.name === "home");
  onHomeRef.current = route.name === "home";

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

  // The detail view always reads the run back from the backend, so what is shown
  // is the persisted record rather than anything held locally.
  //
  // `quiet` is used for poll-driven refreshes: they must not raise the skeleton
  // or clear the view, because a refresh happening every few seconds would make
  // it flicker and a transient failure would blank a run that is fine.
  const loadDetail = useCallback(async (runId: string, options?: { quiet?: boolean }) => {
    const quiet = options?.quiet ?? false;
    const token = ++detailTokenRef.current;
    if (!quiet) {
      setDetailLoading(true);
      setDetailError(null);
    }
    try {
      const detail = await fetchRun(runId);
      if (token !== detailTokenRef.current || selectedIdRef.current !== runId) return;
      setSelectedRun(detail);
    } catch (error) {
      if (token !== detailTokenRef.current || selectedIdRef.current !== runId) return;
      if (quiet) return; // keep showing what we already have
      setSelectedRun(null);
      setDetailError(messageOf(error));
    } finally {
      if (!quiet && token === detailTokenRef.current) setDetailLoading(false);
    }
  }, []);

  const pollOnce = useCallback(
    async (runId: string) => {
      // One request at a time: a slow response must not pile up behind the
      // interval and produce a queue of overlapping fetches.
      if (progressInFlightRef.current) return;
      progressInFlightRef.current = true;
      const token = ++progressTokenRef.current;
      try {
        const next = await fetchRunProgress(runId);
        if (token !== progressTokenRef.current || selectedIdRef.current !== runId) return;
        setProgress(next);

        // Re-fetch the heavy payload only when something actually changed — and
        // that includes any attempt or verification transition, not just the
        // job's stage, which stays "orchestrating" for the whole run.
        const signature = progressSignature(next);
        if (signature !== signatureRef.current) {
          signatureRef.current = signature;
          void loadDetail(runId, { quiet: true });
          // The run list is not rendered on a run page, so refetching 25 runs
          // every tick would be pure waste.
          if (onHomeRef.current) void loadRuns();
        }
      } catch {
        // A failed tick is not worth surfacing: the next one is 2s away, and the
        // manual Refresh reports errors properly.
      } finally {
        progressInFlightRef.current = false;
      }
    },
    [loadDetail, loadRuns],
  );

  useEffect(() => {
    selectedIdRef.current = selectedId;
    // Per-run polling state must never leak across a navigation.
    progressTokenRef.current += 1;
    progressInFlightRef.current = false;
    signatureRef.current = "";
    setProgress(null);
    setActionError(null);

    if (selectedId === null) {
      detailTokenRef.current += 1;
      setSelectedRun(null);
      setDetailError(null);
      return;
    }
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const mergedRun = useMemo(
    () => mergeProgress(selectedRun, progress),
    [selectedRun, progress],
  );

  // Polling runs only while a job is queued or running. When it reaches a
  // terminal state this flips to false and the effect's cleanup clears the
  // interval; navigating away or unmounting does the same.
  const polling = hasActiveJob(progress, selectedRun);

  useEffect(() => {
    if (selectedId === null || !polling) return;
    const timer = window.setInterval(() => {
      void pollOnce(selectedId);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [selectedId, polling, pollOnce]);

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

  const openRun = useCallback((runId: string) => {
    navigate(hrefForRun(runId));
  }, []);

  const handleSubmit = useCallback(
    async (input: RunCreateInput): Promise<boolean> => {
      setSubmitting(true);
      setSubmitError(null);
      setFieldErrors({});
      try {
        const created = await createRun(input);
        setCreatedId(created.id);
        await loadRuns();
        // Creating a run opens it, as it did before — now as a navigation.
        navigate(hrefForRun(created.id));
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

  // Start and Cancel share this shape. The button is disabled while a request is
  // in flight, but that is only a courtesy: the backend enforces idempotency, so
  // a double click cannot create a second job even if the guard is bypassed.
  const runAction = useCallback(
    async (runId: string, action: (id: string) => Promise<unknown>) => {
      const token = ++actionTokenRef.current;
      setActionPending(true);
      setActionError(null);
      try {
        await action(runId);
        if (token !== actionTokenRef.current || selectedIdRef.current !== runId) return;
        // Read the persisted job back rather than assuming what it became.
        signatureRef.current = "";
        await loadDetail(runId, { quiet: true });
        void loadRuns();
        void pollOnce(runId);
      } catch (error) {
        if (token !== actionTokenRef.current || selectedIdRef.current !== runId) return;
        setActionError(messageOf(error));
      } finally {
        if (token === actionTokenRef.current) setActionPending(false);
      }
    },
    [loadDetail, loadRuns, pollOnce],
  );

  const handleStart = useCallback(
    (runId: string) => void runAction(runId, startRun),
    [runAction],
  );
  const handleCancel = useCallback(
    (runId: string) => void runAction(runId, cancelRun),
    [runAction],
  );

  const onRun = route.name === "run";

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead__inner">
          <a className="brand" href={HOME_HREF}>
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
          </a>
          <span className="pill">milestone 6 · start from the dashboard</span>
        </div>
      </header>

      <main className={`main${onRun ? " main--wide" : ""}`}>
        {onRun ? (
          <RunPage
            runId={selectedId ?? ""}
            run={mergedRun}
            runs={runs}
            loading={detailLoading}
            error={detailError}
            justCreated={mergedRun !== null && mergedRun.id === createdId}
            polling={polling}
            actionPending={actionPending}
            actionError={actionError}
            onStart={handleStart}
            onCancel={handleCancel}
            onOpenRun={openRun}
            onRetry={() => {
              if (selectedId !== null) void loadDetail(selectedId);
            }}
            onRefresh={() => {
              if (selectedId !== null) {
                signatureRef.current = "";
                void loadDetail(selectedId);
                void pollOnce(selectedId);
              }
              void loadRuns();
            }}
          />
        ) : (
          <HomePage
            runs={runs}
            listError={listError}
            onReloadRuns={() => void loadRuns()}
            onOpenRun={openRun}
            submitting={submitting}
            submitError={submitError}
            fieldErrors={fieldErrors}
            onSubmit={handleSubmit}
            onDismissFieldError={dismissFieldError}
          />
        )}
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
