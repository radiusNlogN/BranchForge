import type { RunStatus } from "../types";

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`badge badge--${status}`}>
      <span className="badge__dot" aria-hidden="true" />
      {status}
    </span>
  );
}
