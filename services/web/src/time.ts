// Time ranges: relative ("Last 30 minutes", ending now) or custom (fixed from/to), and chart buckets.
export type Range = { key: string; label: string; minutes: number; from?: number; to?: number };  // from/to: epoch ms

export const RANGES: Range[] = [
  { key: "15m", label: "Last 15 minutes", minutes: 15 },
  { key: "30m", label: "Last 30 minutes", minutes: 30 },
  { key: "1h", label: "Last 1 hour", minutes: 60 },
  { key: "6h", label: "Last 6 hours", minutes: 360 },
  { key: "24h", label: "Last 24 hours", minutes: 1440 },
  { key: "7d", label: "Last 7 days", minutes: 10080 },
  { key: "30d", label: "Last 30 days", minutes: 43200 },
];
export const MAX_CUSTOM_DAYS = 31;

/** A fixed period (its key encodes it, so it can be remembered and restored). */
export function customRange(from: number, to: number): Range {
  const d = (t: number) => new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  return { key: `c${from}-${to}`, label: `${d(from)} – ${d(to)}`, minutes: (to - from) / 60_000, from, to };
}

/** A range from its key: a preset, or a custom "c<from>-<to>". */
export function rangeFromKey(key: string | null): Range | undefined {
  const m = key?.match(/^c(\d+)-(\d+)$/);
  return m ? customRange(Number(m[1]), Number(m[2])) : RANGES.find((r) => r.key === key);
}

export function rangeWindow(range: Range, now = Date.now()): { start: string; end: string } {
  if (range.from != null && range.to != null)
    return { start: new Date(range.from).toISOString(), end: new Date(range.to).toISOString() };
  return { start: new Date(now - range.minutes * 60_000).toISOString(), end: new Date(now).toISOString() };
}

/** A bucket size giving ~60-180 points over the range (one the API accepts). */
export function bucketSeconds(range: Range): number {
  const target = (range.minutes * 60) / 120;
  return [10, 30, 60, 300, 900, 1800, 3600, 7200, 21600, 43200, 86400].find((b) => b >= target) ?? 86400;
}

/** Data is kept this many full days plus today (UTC); the API never reads earlier. */
export const RETENTION_DAYS = 30;
/** The oldest moment kept: midnight UTC, RETENTION_DAYS days ago. */
export function keptFrom(now = Date.now()): number {
  const d = new Date(now - RETENTION_DAYS * 86_400_000);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

