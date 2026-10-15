// Number formats of the Kubernetes and GPU pages.
import { fmtNum } from "../time";

export const bytes = (v: number | null | undefined) => {
  if (v == null || !isFinite(v)) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, n = v;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
};
export const mib = (v: number | null | undefined) => (v == null ? "–" : bytes(v * 2 ** 20));
export const cores = (v: number | null | undefined) =>
  v == null ? "–" : v >= 10 ? `${v.toFixed(1)} cores` : v >= 1 ? `${v.toFixed(2)} cores` : `${Math.round(v * 1000)}m`;
export const pct = (v: number | null | undefined, digits = 0) => (v == null || !isFinite(v) ? "–" : `${v.toFixed(digits)}%`);
export const hours = (v: number | null | undefined) =>
  v == null ? "–" : v >= 100 ? `${fmtNum(Math.round(v))} h` : v >= 1 ? `${v.toFixed(1)} h` : `${Math.round(v * 60)} min`;
export const money = (v: number) =>
  v >= 1000 ? `$${fmtNum(Math.round(v))}` : v >= 10 ? `$${v.toFixed(0)}` : `$${v.toFixed(2)}`;
export const ago = (iso: string | null | undefined, now = Date.now()) => {
  if (!iso) return "–";
  const s = Math.max(0, (now - Date.parse(iso)) / 1000);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129_600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};
export const shortGpu = (uuid: string) => (uuid.startsWith("GPU-") ? uuid.slice(4, 12) : uuid);
