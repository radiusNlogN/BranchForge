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
