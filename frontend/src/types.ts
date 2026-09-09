/** Mirrors the backend's `RunRead` schema. */

/** Only `pending` is reachable in this milestone; runs are never executed. */
export type RunStatus = "pending";

export interface Run {
  id: string;
  repository_url: string;
  issue_description: string;
  max_parallel_attempts: number;
  status: RunStatus;
  /** ISO-8601, always UTC (`...Z`). */
  created_at: string;
  updated_at: string;
}

export interface RunCreateInput {
  repository_url: string;
  issue_description: string;
  max_parallel_attempts: number;
}

export const MAX_ISSUE_DESCRIPTION_LENGTH = 10_000;
export const ATTEMPT_CHOICES = [1, 2, 3] as const;
