import type { ReactNode } from "react";

import type { ExecutionJob, PatchAttempt, RunDetail as RunDetailData } from "../types";
import {
  dispatchCommand,
  orchestrateCommand,
  proposeCommand,
  verifyAttemptCommand,
  verifyCommand,
} from "../types";
import { formatAbsolute, formatRelative } from "../time";
import { InspectionPanel } from "./InspectionPanel";
import { OrchestrationPanel } from "./OrchestrationPanel";
import { PatchAttemptPanel } from "./PatchAttemptPanel";
import { VerificationPanel } from "./VerificationPanel";

/**
 * The body of a run page: everything below the header bar.
 *
 * Ordering is deliberate — result first, evidence after. The comparison and the
 * job's state are what a reader wants on arrival; the issue text, the inspection
 * report, and per-attempt logs are reference material and are collapsed or
 * selected rather than stacked.
 *
 * No Refresh button lives in here. There is one, in the page header.
 */

/** Mirrors the backend's start rules, so the button appears only when it would work. */
export function canStart(run: RunDetailData): boolean {
  if (run.job !== null) return false;
  if (run.orchestration !== null) return false;
  if (run.attempts.length > 0) return false;
  if (run.status === "pending") return true;
  if (run.status === "ready") {
    return run.inspection !== null && run.inspection.commit_sha !== null;
  }
  return false;
}

/** Cancel's label, which changes once the request has been recorded. */
export function runActionLabel(job: ExecutionJob): string {
  return job.cancel_requested ? "Cancelling…" : "Cancel";
}

const STAGE_WORDS: Record<string, string> = {
  inspecting: "reading the repository",
  orchestrating: "proposing and verifying patches",
};

/**
 * What each job status means, in words, for the badge.
 *
 * `queued` is the one that matters: the badge must say *why* nothing is
 * happening, not just print the enum. A run sitting at "queued" is not work in
 * progress — it is waiting for a dispatcher that may not be running at all, and
 * the UI is required to keep saying so rather than implying activity.
 */
export const JOB_WORDS: Record<string, string> = {
  queued: "waiting for a dispatcher",
  running: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  interrupted: "interrupted",
};

/** A short issue description needs no disclosure; a long one does. */
const INLINE_DESCRIPTION_LIMIT = 280;

function JobState({ job, polling }: { job: ExecutionJob; polling: boolean }) {
  return (
    <section className="inspection">
      <div className="inspection__header">
        <h3>Execution</h3>
      </div>

      {job.recovery_required ? (
        <div className="callout callout--error" role="alert">
          <strong>This job needs manual recovery.</strong>
          <p>
            It was found running with no dispatcher holding the lock, so the dispatcher that
            started it died. Its processes and containers may still be alive, so nothing was
            resumed and nothing was declared stopped — neither would be true. No further jobs
            will run until this is resolved; see the README&apos;s recovery notes.
          </p>
        </div>
      ) : null}

      {job.status === "queued" ? (
        <div className="empty">
          <p className="empty__title">Queued — nothing is running yet</p>
          <p className="empty__text">
            This job waits until a dispatcher process picks it up. If none is running, start one
            from the <code>backend/</code> directory:
          </p>
          <pre className="command">{dispatchCommand()}</pre>
          <p className="muted small">
            One dispatcher per database, on this machine. It needs <code>ANTHROPIC_API_KEY</code>,
            Docker, and the runner image.
          </p>
        </div>
      ) : null}

      {job.status === "running" ? (
        <p className="muted small">
          A dispatcher is running this job
          {job.stage ? <> — {STAGE_WORDS[job.stage] ?? job.stage}</> : null}.{" "}
          {polling ? "This page is updating itself." : null}
          {job.cancel_requested
            ? " Cancellation was requested; the job stays running until its work has stopped and its containers are confirmed removed."
            : ""}
        </p>
      ) : null}

      {job.status === "cancelled" ? (
        <div className="callout callout--warn" role="note">
          <strong>Cancelled.</strong>
          <p>
            Whatever had already been recorded — the inspection, any patches, any test results —
            is unchanged and shown below. Nothing is resumed; retries are not implemented.
          </p>
        </div>
      ) : null}

      {job.status === "interrupted" ? (
        <div className="callout callout--warn" role="note">
          <strong>The dispatcher stopped while this job was running.</strong>
          <p>{job.error_message}</p>
        </div>
      ) : null}

      {job.status === "failed" ? (
        <div className="callout callout--error" role="alert">
          <strong>This job failed{job.error_kind ? ` (${job.error_kind})` : ""}.</strong>
          <p>{job.error_message}</p>
        </div>
      ) : null}

      {job.notes && job.notes.length > 0 ? (
        <ul className="small muted">
          {job.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}

      {/* Deliberately not repeating the run id here; the header identifies the run. */}
      <p className="muted small">
        Stage {job.stage ? (STAGE_WORDS[job.stage] ?? job.stage) : "—"} ·{" "}
        {job.started_at ? `started ${formatAbsolute(job.started_at)}` : "not started"}
        {job.completed_at ? ` · finished ${formatAbsolute(job.completed_at)}` : ""}
      </p>
    </section>
  );
}

function NotStartedYet({ run }: { run: RunDetailData }) {
  if (!canStart(run)) {
    if (run.status === "failed") {
      return (
        <div className="empty">
          <p className="empty__title">This run cannot be started</p>
          <p className="empty__text">
            Its inspection failed, so there is nothing to work from. Create a new run to try
            again.
          </p>
        </div>
      );
    }
    if (run.status === "inspecting") {
      return (
        <div className="empty">
          <p className="empty__title">Being inspected outside the queue</p>
          <p className="empty__text">
            A worker started by hand is inspecting this run. It can be started once that
            finishes.
          </p>
        </div>
      );
    }
    return null;
  }

  return (
    <div className="empty">
      <p className="empty__title">Ready to start</p>
      <p className="empty__text">
        Start runs {run.max_parallel_attempts} competing attempt
        {run.max_parallel_attempts === 1 ? "" : "s"}
        {run.status === "pending" ? ", after inspecting the repository" : ""}. Each attempt
        proposes a patch and tests it in a container, and the results are then compared.
      </p>
      {/*
        No Start button here: it lives once, in the page header, beside Cancel
        and Refresh. This block used to carry its own copy, which left two
        identical primary buttons on screen doing the same thing.
      */}
      <p className="muted small">
        Use <strong>Start</strong> above. That queues the work — a dispatcher process does it,
        and nothing runs inside the web request. You can still drive each step by hand instead:
      </p>
      <details className="reasons">
        <summary>Run the steps manually</summary>
        <pre className="command">{orchestrateCommand(run.id)}</pre>
        <p className="muted small">Or propose a single patch without competing attempts:</p>
        <pre className="command">{proposeCommand(run.id)}</pre>
      </details>
    </div>
  );
}

/** One attempt's evidence: the proposal beside its verification on wide screens. */
export function AttemptEvidence({
  run,
  attempt,
}: {
  run: RunDetailData;
  attempt: PatchAttempt;
}) {
  const orchestrationActive =
    run.orchestration !== null &&
    (run.orchestration.status === "queued" || run.orchestration.status === "running");
  const command =
    run.attempts.length === 1 && attempt.orchestration_id === null
      ? verifyCommand(run.id)
      : verifyAttemptCommand(attempt.id);
  return (
    <div className="attemptSplit">
      <div className="attemptSplit__col">
        <PatchAttemptPanel
          attempt={attempt}
          heading={
            run.attempts.length > 1
              ? `Proposed patch — attempt ${attempt.attempt_index}`
              : "Proposed patch"
          }
        />
      </div>
      <div className="attemptSplit__col">
        <VerificationPanel
          command={command}
          hasAttempt={attempt.diff !== null}
          pendingByOrchestrator={orchestrationActive && attempt.pipeline_error_kind === null}
          verification={attempt.verification}
        />
      </div>
    </div>
  );
}

interface RunBodyProps {
  run: RunDetailData;
  polling: boolean;
  /** The tab strip and its selected panel, supplied by the page. */
  attemptSlot: ReactNode;
}

/**
 * Start and Cancel are not passed in: they render once, in the page header.
 * This body used to take `onStart`/`actionPending` to drive a second Start
 * button of its own, which put two identical primary buttons on the page.
 */
export function RunBody({ run, polling, attemptSlot }: RunBodyProps) {
  const description = run.issue_description;
  const descriptionIsLong = description.length > INLINE_DESCRIPTION_LIMIT;

  return (
    <>
      {run.job !== null ? (
        <JobState job={run.job} polling={polling} />
      ) : (
        <NotStartedYet run={run} />
      )}

      {/* Result before evidence. */}
      {run.orchestration !== null ? (
        <OrchestrationPanel
          orchestration={run.orchestration}
          attempts={run.attempts}
          cancelRequested={run.job?.cancel_requested ?? false}
        />
      ) : null}

      {attemptSlot}

      <section className="inspection">
        <div className="inspection__header">
          <h3>Issue description</h3>
        </div>
        {descriptionIsLong ? (
          <details className="reasons">
            <summary>{description.slice(0, 120)}…</summary>
            <p className="issueText">{description}</p>
          </details>
        ) : (
          <p className="issueText">{description}</p>
        )}
      </section>

      <InspectionPanel runId={run.id} status={run.status} inspection={run.inspection} />

      {/*
        The run's identity stays visible.

        It was briefly folded into the collapsed block below, which broke the
        page in a way worth recording: a run page that never states which run it
        is showing. The id is the one piece of metadata a reader needs to copy,
        quote in a bug report, or match against a worker command — and a
        `<details>` hides it from assistive technology and find-in-page alike.
        Everything genuinely secondary is still collapsed.
      */}
      <dl className="properties">
        <div className="properties__row">
          <dt>Run ID</dt>
          <dd>
            <code>{run.id}</code>
          </dd>
        </div>
      </dl>

      <details className="reasons">
        <summary>Run metadata</summary>
        <dl className="properties">
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
      </details>
    </>
  );
}
