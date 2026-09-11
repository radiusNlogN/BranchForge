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

/**
 * Patch-attempt (proposal) state, independent of the run's own status and of
 * verification. `queued` is a slot an orchestrator reserved but has not started.
 */
export type AttemptStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted";

export interface AttemptEvent {
  seq: number;
  kind: string;
  summary: string;
  detail: string | null;
  created_at: string;
}

/**
 * One agent attempt. `diff` is a proposal; whether it improved any test is only
 * ever stated by its `verification`.
 */
export interface PatchAttempt {
  id: string;
  run_id: string;
  attempt_index: number;
  /** `null` for a manual (`propose`) attempt. */
  orchestration_id: string | null;
  emphasis_key: string | null;
  emphasis_text: string | null;
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
  /** Set when an orchestrated pipeline stopped before its verification finished. */
  pipeline_error_kind: string | null;
  pipeline_error_message: string | null;
  /** `null` while queued. */
  started_at: string | null;
  completed_at: string | null;
  events: AttemptEvent[];
  events_total: number;
  verification: Verification | null;
}

/** Did the orchestration run to the end? Separate from what its attempts found. */
export type OrchestrationStatus = "queued" | "running" | "completed" | "interrupted" | "failed";

export interface ComparisonCandidate {
  attempt_index: number;
  attempt_id: string;
  eligible: boolean;
  reasons: string[];
  changed_lines: number | null;
  outcome: string | null;
}

export interface OrchestrationComparison {
  rule: string;
  tie_breaker: string;
  complete: boolean;
  recommendation_scope: "all_attempts" | "completed_attempts_only" | null;
  baselines_consistent: boolean | null;
  recommended_attempt_index: number | null;
  recommended_attempt_id: string | null;
  headline: string;
  candidates: ComparisonCandidate[];
  notes: string[];
}

export interface Orchestration {
  id: string;
  run_id: string;
  status: OrchestrationStatus;
  requested_attempts: number;
  concurrency_limit: number;
  effective_concurrency: number;
  model: string;
  commit_sha: string;
  profile: string;
  image_ref: string;
  image_id: string;
  recommended_attempt_index: number | null;
  /** Written once, when the orchestration ends. */
  comparison: OrchestrationComparison | null;
  notes: string[] | null;
  error_kind: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** The run detail endpoint returns the run plus its inspection, attempts, and orchestration. */
export interface RunDetail extends Run {
  inspection: Inspection | null;
  orchestration: Orchestration | null;
  /** Ordered by attempt_index. One element for a manual run. */
  attempts: PatchAttempt[];
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

export function orchestrateCommand(runId: string): string {
  return `uv run python -m app.worker orchestrate --run-id ${runId}`;
}

/** Did the verification run? Separate from what it found. */
export type VerificationStatus = "running" | "completed" | "failed" | "interrupted";

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

/** For a run with exactly one attempt. */
export function verifyCommand(runId: string): string {
  return `uv run python -m app.worker verify --run-id ${runId}`;
}

/** Unambiguous for any run: names the attempt. */
export function verifyAttemptCommand(attemptId: string): string {
  return `uv run python -m app.worker verify --attempt-id ${attemptId}`;
}
