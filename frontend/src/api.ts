/**
 * Thin client for the BranchForge API.
 *
 * All error shapes are normalized here so components never have to reason about
 * FastAPI's 422 payload: a validation response carries `detail` as an array of
 * `{loc, msg}` objects, while other errors carry `detail` as a plain string.
 */

import type { Run, RunCreateInput, RunDetail } from "./types";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

export { API_BASE_URL };

/** Field-level messages keyed by request field name, e.g. `repository_url`. */
export type FieldErrors = Partial<Record<keyof RunCreateInput, string>>;

export class ApiError extends Error {
  readonly status: number;
  readonly fieldErrors: FieldErrors;

  constructor(message: string, status: number, fieldErrors: FieldErrors = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

const FIELD_LABELS: Record<string, string> = {
  repository_url: "Repository URL",
  issue_description: "Issue description",
  max_parallel_attempts: "Max parallel attempts",
  limit: "Limit",
};

interface ValidationItem {
  loc?: unknown;
  msg?: unknown;
}

function isValidationItem(value: unknown): value is ValidationItem {
  return typeof value === "object" && value !== null;
}

/** Strip Pydantic's `Value error, ` prefix so messages read naturally. */
function cleanMessage(raw: string): string {
  return raw.replace(/^(Value error|Assertion failed),\s*/i, "");
}

function fieldNameOf(item: ValidationItem): string | undefined {
  if (!Array.isArray(item.loc)) return undefined;
  const last = item.loc[item.loc.length - 1];
  return typeof last === "string" ? last : undefined;
}

function errorFromPayload(payload: unknown, status: number): ApiError {
  const detail = (payload as { detail?: unknown } | null)?.detail;

  if (typeof detail === "string") {
    return new ApiError(detail, status);
  }

  if (Array.isArray(detail)) {
    const fieldErrors: FieldErrors = {};
    const messages: string[] = [];

    for (const item of detail) {
      if (!isValidationItem(item)) continue;
      const message = cleanMessage(typeof item.msg === "string" ? item.msg : "Invalid value");
      const field = fieldNameOf(item);

      if (field && field in FIELD_LABELS) {
        // Keep the first message per field; later ones are usually redundant.
        if (!(field in fieldErrors)) {
          fieldErrors[field as keyof RunCreateInput] = message;
        }
        messages.push(`${FIELD_LABELS[field]}: ${message}`);
      } else {
        messages.push(message);
      }
    }

    return new ApiError(
      messages.length > 0 ? messages.join(" · ") : "The request was rejected as invalid.",
      status,
      fieldErrors,
    );
  }

  return new ApiError(`Request failed with status ${status}.`, status);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(
      `Could not reach the BranchForge API at ${API_BASE_URL}. Is the backend running?`,
      0,
    );
  }

  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // Non-JSON error body; fall through to the status-based message.
    }
    throw errorFromPayload(payload, response.status);
  }

  return (await response.json()) as T;
}

export function fetchRuns(limit = 25): Promise<Run[]> {
  return request<Run[]>(`/api/runs?limit=${limit}`);
}

export function fetchRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`);
}

export function createRun(input: RunCreateInput): Promise<Run> {
  return request<Run>("/api/runs", {
    method: "POST",
    body: JSON.stringify(input),
  });
}
