/**
 * Merging a poll response into the displayed run.
 *
 * Polling is only worth doing if what comes back actually reaches the screen.
 * The detail payload and the progress payload describe the same run at different
 * resolutions: detail carries the diffs, logs, events, and comparison; progress
 * carries just the statuses, and arrives far more often. `mergeProgress` overlays
 * the fresher statuses onto the heavier snapshot so attempt rows advance between
 * detail fetches instead of sitting still until one happens.
 *
 * The merge is deliberately narrow. It overlays only fields progress actually
 * reports and never invents, clears, or reorders anything — an attempt's diff,
 * events, and verification evidence keep coming from the detail fetch. A status
 * shown here is always one the backend persisted; nothing is predicted or
 * animated ahead of the data.
 */

import type { AttemptProgress, PatchAttempt, RunDetail, RunProgress } from "./types";

/**
 * The detail snapshot with progress statuses overlaid.
 *
 * Returns the original object when there is nothing to merge, so React's identity
 * check still short-circuits a re-render.
 */
export function mergeProgress(
  run: RunDetail | null,
  progress: RunProgress | null,
): RunDetail | null {
  if (run === null) return null;
  if (progress === null || progress.id !== run.id) return run;

  const byIndex = new Map<number, AttemptProgress>(
    progress.attempts.map((attempt) => [attempt.attempt_index, attempt]),
  );

  const attempts: PatchAttempt[] = run.attempts.map((attempt) => {
    const fresh = byIndex.get(attempt.attempt_index);
    if (fresh === undefined) return attempt;
    return {
      ...attempt,
      status: fresh.status,
      error_kind: fresh.error_kind,
      pipeline_error_kind: fresh.pipeline_error_kind,
      events_total: fresh.events_total,
      started_at: fresh.started_at,
      completed_at: fresh.completed_at,
      // A verification the detail fetch has not seen yet still shows its state;
      // its evidence (logs, comparison, counts) arrives with the next detail.
      verification:
        fresh.verification === null
          ? attempt.verification
          : attempt.verification === null
            ? null
            : {
                ...attempt.verification,
                status: fresh.verification.status,
                outcome: fresh.verification.outcome,
              },
    };
  });

  return {
    ...run,
    status: progress.status,
    updated_at: progress.updated_at,
    job: progress.job,
    orchestration:
      run.orchestration === null || progress.orchestration === null
        ? run.orchestration
        : {
            ...run.orchestration,
            status: progress.orchestration.status,
            recommended_attempt_index: progress.orchestration.recommended_attempt_index,
            started_at: progress.orchestration.started_at,
            completed_at: progress.orchestration.completed_at,
          },
    attempts,
  };
}

/**
 * A compact string that changes whenever something worth re-fetching changed.
 *
 * Used to decide when the heavy detail payload is worth requesting again. It
 * covers the run's status, the job's status and stage, the orchestration, and
 * **every attempt's** status and verification state — because a job sits in stage
 * "orchestrating" for its whole life while individual proposals and verifications
 * come and go underneath it. Watching only the stage would leave attempt rows
 * frozen for the entire run.
 */
export function progressSignature(progress: RunProgress | null): string {
  if (progress === null) return "";
  const job = progress.job;
  const orchestration = progress.orchestration;
  const parts: string[] = [
    progress.status,
    job === null ? "-" : `${job.status}/${job.stage ?? "-"}/${job.cancel_requested}/${job.recovery_required}`,
    orchestration === null
      ? "-"
      : `${orchestration.status}/${orchestration.recommended_attempt_index ?? "-"}`,
  ];
  for (const attempt of progress.attempts) {
    parts.push(
      `${attempt.attempt_index}:${attempt.status}:${attempt.pipeline_error_kind ?? "-"}:` +
        `${attempt.verification?.status ?? "-"}:${attempt.verification?.outcome ?? "-"}`,
    );
  }
  return parts.join("|");
}

/** Does this run have a job a dispatcher will still act on? */
export function hasActiveJob(progress: RunProgress | null, run: RunDetail | null): boolean {
  const job = progress?.job ?? run?.job ?? null;
  if (job === null) return false;
  return job.status === "queued" || job.status === "running";
}
