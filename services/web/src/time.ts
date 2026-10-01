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
  if (Number.isInteger(n) || Math.abs(n) >= 100) return Math.round(n).toLocaleString();
  return Number(n.toPrecision(3)).toLocaleString();   // e.g. 0.372, 12.5
}

export function fmtMs(ns: number): string {
  const ms = ns / 1e6;
  if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
  if (ms >= 10) return ms.toFixed(0) + " ms";
  if (ms >= 1) return ms.toFixed(1) + " ms";
  return ms.toFixed(2) + " ms";
}

/** Data is kept this many full days plus today (UTC); the API never reads earlier. */
export const RETENTION_DAYS = 30;
/** The oldest moment kept: midnight UTC, RETENTION_DAYS days ago. */
export function keptFrom(now = Date.now()): number {
  const d = new Date(now - RETENTION_DAYS * 86_400_000);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}
export const fmtDay = (t: number) => new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });

/** A value in a unit, as people read it: 1.2 s, 3 min, 2 days, 4 years; 300 MB; 99.5%. */
export function fmtUnit(v: number, unit = "", decimals?: number): string {
  const n = (x: number) => (decimals != null ? x.toFixed(decimals) : fmtNum(x));
  const scaled = (x: number, steps: [number, string][]) => {
    const [div, name] = steps.find(([d]) => Math.abs(x) >= d) ?? steps[steps.length - 1];
    return `${n(x / div)} ${name}`;
  };
  if (!isFinite(v)) return String(v);
  if (unit === "ms" || unit === "s" || unit === "ns") {
    const ms = unit === "s" ? v * 1000 : unit === "ns" ? v / 1e6 : v;
    if (ms === 0) return "0";
    if (Math.abs(ms) < 0.001) return `${n(ms * 1e6)} ns`;
    if (Math.abs(ms) < 1) return `${n(ms * 1000)} µs`;
    return scaled(ms, [[31_536_000_000, "years"], [2_592_000_000, "months"], [86_400_000, "days"], [3_600_000, "h"], [60_000, "min"], [1000, "s"], [1, "ms"]]);
  }
  if (unit === "bytes") return v === 0 ? "0" : scaled(v, [[1e12, "TB"], [1e9, "GB"], [1e6, "MB"], [1e3, "KB"], [1, "bytes"]]);
  if (unit === "%") return `${n(v)}%`;
  if (unit === "percentunit") return `${n(v * 100)}%`;
  if (unit === "/s") return `${n(v)}/s`;
  return n(v);
}
