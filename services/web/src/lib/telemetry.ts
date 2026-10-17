// What telemetry values mean, shared by the pages: log severity ranges and span status.
import type { StackKey } from "../components/Charts";

/** Log severities (OTLP severity_number) in the bands the pages chart and filter by. */
export const SEVERITY_RANGES: (StackKey & { min: number; max: number; key: string })[] = [
  { key: "error", label: "ERROR & FATAL", color: "var(--sev-error)", min: 17, max: 24 },
  { key: "warn", label: "WARN", color: "var(--sev-warn)", min: 13, max: 16 },
  { key: "info", label: "INFO", color: "var(--sev-info)", min: 9, max: 12 },
  { key: "debug", label: "TRACE & DEBUG", color: "var(--sev-debug)", min: 1, max: 8 },
  { key: "unknown", label: "UNKNOWN", color: "var(--surface-3)", min: 0, max: 0 },
];
export const severityRange = (n: unknown) => {
  const v = Number(n) || 0;
  const i = SEVERITY_RANGES.findIndex((r) => v >= r.min && v <= r.max);
  return i < 0 ? SEVERITY_RANGES.length - 1 : i;
};

/** OTLP span status: stored as its number (2 = error); older data may hold the enum name. */
export const isError = (status?: string) => status === "2" || status === "STATUS_CODE_ERROR";
