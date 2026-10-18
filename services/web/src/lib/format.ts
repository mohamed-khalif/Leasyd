// Every number, size, percent, duration and date the app shows is formatted here (see
// docs/formatting.md); pages don't format on their own (lint: no toFixed / toLocale* / Intl).
import type { Range } from "../time";

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

/** A value in a unit, as people read it: 1.2 s, 3 min, 2 days, 4 years; 300 MB; 99.5%. */
export function fmtUnit(v: number, unit = "", decimals?: number): string {
  const places = decimals == null || !isFinite(decimals) ? null : Math.min(20, Math.max(0, Math.round(decimals)));   // toFixed throws outside 0-100
  const n = (x: number) => (places != null ? x.toFixed(places) : fmtNum(x));
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
  if (unit === "W" || unit === "°C") return `${n(v)} ${unit}`;
  return n(v);
}

export function fmtTime(iso: string, range?: Range): string {
  const d = new Date(iso);
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", hour12: false };
  if (range && range.minutes <= 15) opts.second = "2-digit";
  if (range && range.minutes > 1440) return d.toLocaleString([], { month: "short", day: "numeric", ...opts });
  return d.toLocaleTimeString([], opts);
}

export const fmtDay = (t: number) => new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });

/** A timestamp as local "YYYY-MM-DD hh:mm:ss.mmm" (the text itself when it isn't one). */
export function fmtTs(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  const p = (n: number, l = 2) => String(n).padStart(l, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** Shown for a value that is missing or not a number. */
export const NONE = "–";
const missing = (v: number | null | undefined): v is null | undefined => v == null || !Number.isFinite(v);
/** toFixed without trailing zeros: 12.50 -> "12.5", 12.00 -> "12". */
const trim = (v: number, digits: number) => String(Number(v.toFixed(digits)));

/** An exact count with thousands separators: 1,234,567. */
export const fmtInt = (n: number | null | undefined) => (missing(n) ? NONE : Math.round(n).toLocaleString());

/** A fixed number of decimals (clamped to 0-20, which toFixed accepts). */
export const fmtFixed = (v: number | null | undefined, digits: number) =>
  missing(v) ? NONE : v.toFixed(Math.min(20, Math.max(0, Math.round(Number.isFinite(digits) ? digits : 0))));

/** v in 0-100. 2 decimals below 1% and from 90% (99.96% is never "100%"), else 1; no trailing zeros. */
export function fmtPercent(v: number | null | undefined, digits?: number): string {
  if (missing(v)) return NONE;
  const d = digits ?? (v === 0 || v === 100 ? 0 : Math.abs(v) < 1 || (v >= 90 && v < 100) ? 2 : 1);
  let out = trim(v, d);
  if (v < 100 && Number(out) >= 100) out = trim(v, d + 2);   // never round up to 100%
  return `${out}%`;
}
/** v as a fraction (0-1), shown as a percent. */
export const fmtRatio = (v: number | null | undefined, digits?: number) => (missing(v) ? NONE : fmtPercent(v * 100, digits));

/** Milliseconds as people read them: 850 ms, 1.23 s, 4 min, 2.5 h, 3 days. */
export function fmtDuration(ms: number | null | undefined): string {
  if (missing(ms)) return NONE;
  if (Math.abs(ms) < 1000) return fmtMs(ms * 1e6);
  if (Math.abs(ms) < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  if (Math.abs(ms) < 90 * 60_000) return `${Math.round(ms / 60_000)} min`;
  if (Math.abs(ms) < 36 * 3_600_000) return `${trim(ms / 3_600_000, 1)} h`;
  return `${Math.round(ms / 86_400_000)} days`;   // 36 h or more: 2 days or more
}

/** How long ago: just now (under 10 s), 45 s ago, 12 min ago, 5 h ago, 3 days ago. */
export function fmtAgo(t: string | number | null | undefined, now = Date.now()): string {
  if (t == null || t === "") return NONE;
  const at = typeof t === "number" ? t : Date.parse(t);
  if (!Number.isFinite(at)) return NONE;
  const s = Math.max(0, (now - at) / 1000);
  if (s < 10) return "just now";
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129_600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/** A moment within the visible range: 14:05:07. */
export const fmtClock = (t: string | number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
/** A moment that may be on another day: 17 Oct, 14:05 (order follows the viewer's locale). */
export const fmtDateTime = (t: string | number, seconds = false) =>
  new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false });

/** Prices per unit: 2 decimals, or 3 when the third isn't zero ($0.18, $0.075, $3.43). */
export const fmtUnitPrice = (v: number) => `$${Math.round(v * 1000) % 10 ? v.toFixed(3) : v.toFixed(2)}`;

// Sizes, CPU, hours and money
export const fmtBytes = (v: number | null | undefined) => {
  if (v == null || !isFinite(v)) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, n = v;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : trim(n, 1)} ${units[i]}`;
};
export const fmtMiB = (v: number | null | undefined) => (v == null ? NONE : fmtBytes(v * 2 ** 20));
export const fmtCores = (v: number | null | undefined) =>
  v == null ? "–" : v >= 10 ? `${v.toFixed(1)} cores` : v >= 1 ? `${v.toFixed(2)} cores` : `${Math.round(v * 1000)}m`;
export const fmtHours = (v: number | null | undefined) =>
  v == null ? "–" : v >= 100 ? `${fmtNum(Math.round(v))} h` : v >= 1 ? `${v.toFixed(1)} h` : `${Math.round(v * 60)} min`;
export const fmtUsd = (v: number) =>
  v >= 1000 ? `$${fmtNum(Math.round(v))}` : v >= 10 ? `$${v.toFixed(0)}` : `$${v.toFixed(2)}`;
export const shortGpu = (uuid: string) => (uuid.startsWith("GPU-") ? uuid.slice(4, 12) : uuid);
