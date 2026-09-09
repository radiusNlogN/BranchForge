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

/** Patch-attempt state, independent of the run's own status. */
export type AttemptStatus = "running" | "succeeded" | "failed";

export interface AttemptEvent {
  seq: number;
  kind: string;
  summary: string;
  detail: string | null;
  created_at: string;
}

/**
 * One agent attempt. `diff` is an UNVERIFIED proposal — it was never applied and
 * no tests were run.
 */
export interface PatchAttempt {
  id: string;
  run_id: string;
  status: AttemptStatus;
  model: string;
  commit_sha: string | null;
  diff: string | null;
  summary: string | null;
  suggested_test_command: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error_kind: string | null;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
  events: AttemptEvent[];
  events_total: number;
}

/** The run detail endpoint returns the run plus its inspection and attempt. */
export interface RunDetail extends Run {
  inspection: Inspection | null;
  patch_attempt: PatchAttempt | null;
  verification: Verification | null;
}

export const MAX_ISSUE_DESCRIPTION_LENGTH = 10_000;
export const ATTEMPT_CHOICES = [1, 2, 3] as const;

/** Shown so the user can copy the exact command for a run. */
export function workerCommand(runId: string): string {
  return `uv run python -m app.worker inspect --run-id ${runId}`;
}

export function proposeCommand(runId: string): string {
  return `uv run python -m app.worker propose --run-id ${runId}`;
}

/** Did the verification run? Separate from what it found. */
export type VerificationStatus = "running" | "completed" | "failed";

/** One classified container run. */
export interface RunSummaryData {
  kind: string;
  exit_code: number | null;
  collected: string[];
  outcomes: Record<string, string>;
  counts: Record<string, number>;
  collect_errors: { nodeid: string; message: string }[];
  report_error: string | null;
  truncated: boolean;
  duration_seconds: number;
  timed_out: boolean;
}

/** The baseline-versus-patched finding. */
export interface ComparisonData {
  outcome: string;
  fixed: string[];
  still_failing: string[];
  regressions: string[];
  no_longer_exercised: string[];
  weakened: string[];
  missing_from_patched: string[];
  added_in_patched: string[];
  detail: string;
}

/**
 * One verification. `status` says whether it ran; `outcome` says what it found —
 * deliberately not a single pass/fail flag, because "the verification completed"
 * and "the patch fixes the bug" are different claims.
 */
export interface Verification {
  id: string;
  attempt_id: string;
  status: VerificationStatus;
  outcome: string | null;
  detail: string | null;
  commit_sha: string | null;
  patch_sha256: string | null;
  profile: string | null;
  image_ref: string | null;
  image_id: string | null;
  runner_args: string[] | null;
  patch_applied: boolean;
  patch_apply_message: string | null;
  patch_touched_tests: boolean;
  files_changed: string[] | null;
  baseline_summary: RunSummaryData | null;
  patched_summary: RunSummaryData | null;
  comparison: ComparisonData | null;
  baseline_log: string | null;
  patched_log: string | null;
  supplemental_summary: RunSummaryData | null;
  supplemental_log: string | null;
  notes: string[] | null;
  error_kind: string | null;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
}

export function verifyCommand(runId: string): string {
  return `uv run python -m app.worker verify --run-id ${runId}`;
}
