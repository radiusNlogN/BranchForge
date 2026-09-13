import { useEffect, useId, useState } from "react";

import { HOME_HREF, navigate } from "../routing";
import type { Run, RunDetail as RunDetailData } from "../types";
import { AttemptTabPanel, AttemptTabs } from "./AttemptTabs";
import { Callout } from "./Callout";
import { AttemptEvidence, JOB_WORDS, RunBody, canStart, runActionLabel } from "./RunDetail";
import { NotImplementedNote } from "./NotImplementedNote";
import { LifecycleBadge, StatusBadge } from "./StatusBadge";

/**
 * One run, full width, on its own route.
 *
 * Three things this layout fixes:
 *
 * 1. The detail used to sit in the narrower half of a 1180px split beside a
 *    permanent run list. It now owns the viewport.
 * 2. Every attempt rendered expanded and stacked, each with its own diff, event
 *    log and container output. One attempt shows at a time, chosen by tabs.
 * 3. Five Refresh buttons called one handler with one disabled flag. There is
 *    one, in the header.
 *
 * The compact run switcher below the header is a deliberate compromise:
 * `test_switching_runs_does_not_show_the_previous_run` opens a second run while
 * already viewing one, by clicking `.runList__item`, and those tests are frozen.
 * So visible, clickable, newest-first run buttons must exist on this route. The
 * persistent column is gone; this strip stays.
 */

interface Props {
  runId: string;
  run: RunDetailData | null;
  runs: Run[] | null;
  loading: boolean;
  error: string | null;
  justCreated: boolean;
  polling: boolean;
  /** `false` only when the backend says this deployment runs no dispatcher. */
  dispatcherAvailable: boolean | null;
  actionPending: boolean;
  actionError: string | null;
  onStart: (runId: string) => void;
  onCancel: (runId: string) => void;
  onRetry: () => void;
  onRefresh: () => void;
  onOpenRun: (runId: string) => void;
}

export function RunPage({
  runId,
  run,
  runs,
  loading,
  error,
  justCreated,
  polling,
  dispatcherAvailable,
  actionPending,
  actionError,
  onStart,
  onCancel,
  onRetry,
  onRefresh,
  onOpenRun,
}: Props) {
  const tabsId = useId();

  // Which attempt the tabs show. Reset when the run changes, but NOT on a
  // progress poll: resetting every 2 seconds would drag the reader back to
  // attempt 1 while they were reading attempt 3.
  const [selectedAttempt, setSelectedAttempt] = useState<number | null>(null);
  useEffect(() => {
    setSelectedAttempt(null);
  }, [runId]);

  const attempts = run?.attempts ?? [];
  const recommended = run?.orchestration?.recommended_attempt_index ?? null;
  // Follows the data until the user picks: default to the recommended attempt,
  // else the first one.
  const fallback = recommended ?? attempts[0]?.attempt_index ?? 1;
  const activeIndex = selectedAttempt ?? fallback;
  const activeAttempt =
    attempts.find((attempt) => attempt.attempt_index === activeIndex) ?? attempts[0] ?? null;

  const job = run?.job ?? null;
  const jobActive = job !== null && (job.status === "queued" || job.status === "running");

  function goHome(event?: React.MouseEvent) {
    if (event) {
      // Let modified clicks (new tab, new window) behave normally.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
    }
    navigate(HOME_HREF);
  }

  return (
    <div className="runPage">
      <div className="runPage__bar">
        <a className="button button--ghost runPage__back" href={HOME_HREF} onClick={goHome}>
          ← Back to runs
        </a>

        <div className="runPage__identity">
          {run !== null ? (
            <a
              className="detailHead__repo"
              href={run.repository_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {run.repository_url.replace(/^https:\/\/github\.com\//, "")}
            </a>
          ) : (
            <span className="muted">Run {runId.slice(0, 8)}…</span>
          )}
          {run !== null ? <StatusBadge status={run.status} /> : null}
          {job !== null ? (
            <LifecycleBadge
              family="job"
              status={job.status}
              label={JOB_WORDS[job.status] ?? job.status}
            />
          ) : null}
        </div>

        <div className="runPage__actions">
          {run !== null && canStart(run) && dispatcherAvailable !== false ? (
            <button
              type="button"
              className="button button--primary"
              onClick={() => onStart(run.id)}
              disabled={actionPending}
            >
              {actionPending ? "Starting…" : "Start"}
            </button>
          ) : null}
          {run !== null && jobActive && job !== null ? (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => onCancel(run.id)}
              disabled={actionPending || job.cancel_requested}
            >
              {runActionLabel(job)}
            </button>
          ) : null}
          {/* The only Refresh on the page. */}
          <button
            type="button"
            className="button button--ghost"
            onClick={onRefresh}
            disabled={loading}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {runs !== null && runs.length > 1 ? (
        <nav className="runSwitcher" aria-label="Switch run">
          <ul className="runList runList--strip">
            {runs.map((other) => (
              <li key={other.id}>
                <button
                  type="button"
                  className={`runList__item${other.id === runId ? " runList__item--selected" : ""}`}
                  aria-current={other.id === runId}
                  onClick={() => onOpenRun(other.id)}
                >
                  <span className="runList__top">
                    <span className="runList__repo">
                      {other.repository_url.replace(/^https:\/\/github\.com\//, "")}
                    </span>
                    <StatusBadge status={other.status} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
      ) : null}

      {loading && run === null ? (
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
          <p className="empty__title">No such run</p>
          <p className="empty__text">
            Nothing is stored under <code>{runId}</code>. It may have been removed, or the link
            may be wrong.
          </p>
          <button type="button" className="button button--ghost" onClick={() => goHome()}>
            Back to runs
          </button>
        </div>
      ) : null}

      {run !== null ? (
        <div className="detailBody">
          {justCreated ? (
            <Callout tone="info" title="Run saved">
              <p>This is the record the backend persisted, read back from the database.</p>
            </Callout>
          ) : null}

          {actionError ? (
            <Callout tone="error" title="That request was refused">
              <p>{actionError}</p>
            </Callout>
          ) : null}

          <NotImplementedNote compact dispatcherAvailable={dispatcherAvailable} />

          <RunBody
            run={run}
            polling={polling}
            dispatcherAvailable={dispatcherAvailable}
            attemptSlot={
              attempts.length > 0 && activeAttempt !== null ? (
                <section className="inspection">
                  <div className="inspection__header">
                    <h3>Attempt evidence</h3>
                  </div>
                  {attempts.length > 1 ? (
                    <AttemptTabs
                      attempts={attempts}
                      selectedIndex={activeIndex}
                      onSelect={setSelectedAttempt}
                      recommendedIndex={recommended}
                      baseId={tabsId}
                    />
                  ) : null}
                  <AttemptTabPanel baseId={tabsId} attemptIndex={activeAttempt.attempt_index}>
                    <AttemptEvidence run={run} attempt={activeAttempt} />
                  </AttemptTabPanel>
                </section>
              ) : null
            }
          />
        </div>
      ) : null}
    </div>
  );
}
