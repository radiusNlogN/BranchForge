/** Mirrors the backend's `RunRead` schema. */

/**
 * Run lifecycle. `ready` means repository inspection finished — not that a fix
 * or patch exists.
 */
export type RunStatus = "pending" | "inspecting" | "ready" | "failed";

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

/** Mirrors the backend's inspection report schemas. */

export interface RepositoryFacts {
  full_name: string | null;
  name: string | null;
  description: string | null;
  default_branch: string | null;
  commit_sha: string | null;
  is_private: boolean | null;
  is_fork: boolean | null;
  is_archived: boolean | null;
}

export interface FileEntry {
  path: string;
  size: number | null;
}

export interface FilePreview {
  path: string;
  size: number | null;
  /** Untrusted repository text. Render as plain text only. */
  content: string | null;
  bytes_shown: number;
  content_truncated: boolean;
  omitted: boolean;
  omitted_reason: string | null;
  is_binary: boolean;
}

export interface PytestAssessment {
  is_python_project: boolean;
  uses_pytest: boolean;
  python_evidence: string[];
  pytest_evidence: string[];
  test_locations: string[];
  caveat: string;
}

export interface BudgetUsage {
  requests_made: number;
  max_requests: number;
  files_in_tree: number;
  files_listed: number;
  max_files_listed: number;
  files_fetched: number;
  max_files_fetched: number;
  content_bytes_stored: number;
  max_total_content_bytes: number;
  max_file_bytes: number;
}

export interface TruncationInfo {
  tree_truncated: boolean;
  file_listing_truncated: boolean;
  omitted_files: string[];
  notes: string[];
}

export interface InspectionReport {
  repository: RepositoryFacts;
  files: FileEntry[];
  readme: FilePreview | null;
  python_config_files: FilePreview[];
  assessment: PytestAssessment;
  budgets: BudgetUsage;
  truncation: TruncationInfo;
}

export interface Inspection {
  id: string;
  run_id: string;
  commit_sha: string | null;
  default_branch: string | null;
  repository_name: string | null;
  repository_description: string | null;
  tree_truncated: boolean;
  report: InspectionReport | null;
  started_at: string;
  completed_at: string | null;
  error_kind: string | null;
  error_message: string | null;
}

/** The run detail endpoint returns the run plus its inspection. */
export interface RunDetail extends Run {
  inspection: Inspection | null;
}

export const MAX_ISSUE_DESCRIPTION_LENGTH = 10_000;
export const ATTEMPT_CHOICES = [1, 2, 3] as const;

/** Shown so the user can copy the exact command for a run. */
export function workerCommand(runId: string): string {
  return `uv run python -m app.worker inspect --run-id ${runId}`;
}
