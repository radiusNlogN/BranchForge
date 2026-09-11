import type { RunStatus } from "../types";

const LABELS: Record<RunStatus, string> = {
  pending: "pending",
  inspecting: "inspecting",
  ready: "ready",
  failed: "failed",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`badge badge--${status}`}>
      <span className="badge__dot" aria-hidden="true" />
      {LABELS[status] ?? status}
    </span>
  );
}

/**
 * A lifecycle badge for an attempt, verification, or orchestration. It states
 * whether something *ran*, never whether a patch is good — outcomes are words.
 */
export function LifecycleBadge({
  family,
  status,
  label,
}: {
  family: "attempt" | "verify" | "orch";
  status: string;
  label?: string;
}) {
  return (
    <span className={`badge badge--${family}-${status}`}>
      <span className="badge__dot" aria-hidden="true" />
      {label ?? status}
    </span>
  );
}
