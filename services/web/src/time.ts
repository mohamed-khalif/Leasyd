// Time ranges ("Last 30 minutes") and chart buckets.
export type Range = { key: string; label: string; minutes: number };

export const RANGES: Range[] = [
  { key: "15m", label: "Last 15 minutes", minutes: 15 },
  { key: "30m", label: "Last 30 minutes", minutes: 30 },
  { key: "1h", label: "Last 1 hour", minutes: 60 },
  { key: "6h", label: "Last 6 hours", minutes: 360 },
  { key: "24h", label: "Last 24 hours", minutes: 1440 },
  { key: "7d", label: "Last 7 days", minutes: 10080 },
];

export function rangeWindow(range: Range, now = Date.now()): { start: string; end: string } {
  return { start: new Date(now - range.minutes * 60_000).toISOString(), end: new Date(now).toISOString() };
}

/** A bucket size giving ~60-180 points over the range (one the API accepts). */
export function bucketSeconds(range: Range): number {
  const target = (range.minutes * 60) / 120;
  return [10, 30, 60, 300, 900, 3600, 86400].find((b) => b >= target) ?? 86400;
}

export function fmtTime(iso: string, range?: Range): string {
  const d = new Date(iso);
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", hour12: false };
  if (range && range.minutes <= 15) opts.second = "2-digit";
  if (range && range.minutes > 1440) return d.toLocaleString([], { month: "short", day: "numeric", ...opts });
  return d.toLocaleTimeString([], opts);
}

export function fmtNum(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K";
  return Math.round(n).toLocaleString();
}

export function fmtMs(ns: number): string {
  const ms = ns / 1e6;
  if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
  if (ms >= 10) return ms.toFixed(0) + " ms";
  if (ms >= 1) return ms.toFixed(1) + " ms";
  return ms.toFixed(2) + " ms";
}
