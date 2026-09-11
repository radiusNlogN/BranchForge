/** Timestamp formatting. Backend timestamps are always UTC ISO-8601. */

export function formatAbsolute(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

const UNITS: Array<[limitSeconds: number, perUnit: number, name: string]> = [
  [60, 1, "second"],
  [3600, 60, "minute"],
  [86400, 3600, "hour"],
  [2592000, 86400, "day"],
];

/**
 * Elapsed time between two timestamps, e.g. "3m 04s". With no end, measures to
 * `now` — callers must label that as "so far", because nothing ticks: the value
 * is only as fresh as the last Refresh.
 */
export function formatDuration(
  startIso: string | null,
  endIso: string | null,
  now: Date = new Date(),
): string {
  if (startIso === null) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso === null ? now.getTime() : new Date(endIso).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return "—";
  const total = Math.max(0, Math.round((end - start) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

export function formatRelative(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";

  const seconds = Math.round((now.getTime() - date.getTime()) / 1000);
  if (seconds < 10) return "just now";

  for (const [limit, perUnit, name] of UNITS) {
    if (seconds < limit) {
      const value = Math.floor(seconds / perUnit);
      return `${value} ${name}${value === 1 ? "" : "s"} ago`;
    }
  }
  return formatAbsolute(iso);
}
