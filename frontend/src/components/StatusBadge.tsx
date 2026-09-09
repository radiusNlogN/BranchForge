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
