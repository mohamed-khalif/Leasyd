import { FormEvent, useMemo, useState } from "react";
import { Query, records, Where } from "../api";
import type { Ctx } from "../App";
import { Loads, Panel } from "../components/Panel";
import { TimeSeries } from "../components/TimeSeries";
import { bucketSeconds, fmtNum, rangeWindow } from "../time";
import { useQuery } from "../useQuery";
import { SEVERITY_COLORS, toSeries } from "./Insights";

// OpenTelemetry severity numbers: 5-8 debug, 9-12 info, 13-16 warn, 17-20 error, 21-24 fatal.
const LEVELS = [
  { key: "all", label: "All", min: 0 },
  { key: "debug", label: "Debug+", min: 5 },
  { key: "info", label: "Info+", min: 9 },
  { key: "warn", label: "Warn+", min: 13 },
  { key: "error", label: "Error+", min: 17 },
];
const LIMIT = 200;

export function Logs({ ctx, params }: { ctx: Ctx; params: URLSearchParams }) {
  const [text, setText] = useState(params.get("q") ?? "");
  const [applied, setApplied] = useState(params.get("q") ?? "");
  const [level, setLevel] = useState(params.get("level") ?? "all");
  const [service, setService] = useState(params.get("service") ?? "");
  const [open, setOpen] = useState<number | null>(null);

  const w = useMemo(() => rangeWindow(ctx.range), [ctx.range, ctx.tick]);
  const where: Where[] = [];
  const min = LEVELS.find((l) => l.key === level)?.min ?? 0;
  if (min) where.push({ field: "severity_number", op: ">=", value: min });
  if (applied.trim()) where.push({ field: "body", op: "contains", value: applied.trim() });
  const base: Omit<Query, "group_by" | "aggs" | "search" | "limit"> = {
    signal: "logs", ...w, where, ...(service ? { services: [service] } : {}),
  };
  const key = JSON.stringify(base) + ctx.tick;
  const b = bucketSeconds(ctx.range);
  const hist = useQuery({ ...base, group_by: [`ts:${b}`, "severity_text"], aggs: [{ fn: "count" }], limit: 10000 }, "h" + key);
  const rows = useQuery({ ...base, search: { limit: LIMIT } }, "r" + key);
  const services = useQuery({ signal: "logs", ...w, group_by: ["service"], aggs: [{ fn: "count" }], limit: 100 }, "s" + ctx.range.key + ctx.tick);

  const total = hist.data ? records(hist.data).reduce((a, r) => a + Number(r.count), 0) : 0;
  const lines = rows.data ? records(rows.data) : [];
  const submit = (e: FormEvent) => { e.preventDefault(); setApplied(text); setOpen(null); };

  return (
    <>
      <form className="toolbar" onSubmit={submit}>
        <input className="input grow mono" placeholder="Search log messages…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Search log messages" />
        <select className="select" value={service} onChange={(e) => { setService(e.target.value); setOpen(null); }} aria-label="Service">
          <option value="">All services</option>
          {(services.data ? records(services.data) : []).map((r) => <option key={String(r.service)} value={String(r.service)}>{String(r.service)}</option>)}
        </select>
        <div className="chips" role="radiogroup" aria-label="Minimum severity">
          {LEVELS.map((l) => (
            <button type="button" key={l.key} className={`chip${level === l.key ? " on" : ""}`} role="radio" aria-checked={level === l.key}
                    onClick={() => { setLevel(l.key); setOpen(null); }}>{l.label}</button>
          ))}
        </div>
        <button className="btn primary">Search</button>
      </form>

      <Panel title="Log volume" right={hist.data && <span className="faint mono">{fmtNum(total)} records</span>}>
        <Loads q={hist} empty={!total} height={150}>
          {() => <TimeSeries series={toSeries(records(hist.data!), "severity_text", 6, SEVERITY_COLORS)} range={ctx.range} height={130} />}
        </Loads>
      </Panel>

      <Panel title="Log records" flush
             right={rows.data && <span className="faint">{lines.length >= LIMIT ? `newest ${LIMIT}` : `${lines.length} records`}</span>}>
        <Loads q={rows} empty={!lines.length} height={200}>
          {() => (
            <div>
              {lines.map((r, i) => {
                const sev = String(r.severity_text ?? "").toLowerCase() || sevName(Number(r.severity_number));
                return (
                  <div key={i}>
                    <div className={`logrow${open === i ? " open" : ""}`} onClick={() => setOpen(open === i ? null : i)}>
                      <span className="faint">{fmtTs(String(r.ts))}</span>
                      <span className={`sev ${sev}`}>{sev.toUpperCase() || "—"}</span>
                      <span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{String(r.service)}</span>
                      <span className="body">{String(r.body ?? "")}</span>
                    </div>
                    {open === i && <Detail r={r} go={ctx.go} />}
                  </div>
                );
              })}
            </div>
          )}
        </Loads>
      </Panel>
    </>
  );
}

function Detail({ r, go }: { r: Record<string, unknown>; go: (h: string) => void }) {
  const flat: [string, unknown][] = [];
  for (const [k, v] of Object.entries(r)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const [k2, v2] of Object.entries(v as Record<string, unknown>)) flat.push([`${k === "resource_attributes" ? "resource" : k}.${k2}`, v2]);
    } else flat.push([k, v]);
  }
  return (
    <div className="logdetail">
      <div className="kv">
        {flat.filter(([, v]) => v != null && v !== "").map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <span className="k">{k}</span>
            <span className="v">{k === "trace_id"
              ? <a href={`#/traces/${v}`} onClick={(e) => { e.preventDefault(); go(`/traces/${v}`); }}>{String(v)}</a>
              : String(v)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function sevName(n: number): string {
  if (n >= 21) return "fatal"; if (n >= 17) return "error"; if (n >= 13) return "warn";
  if (n >= 9) return "info"; if (n >= 5) return "debug"; return n ? "trace" : "";
}

function fmtTs(iso: string): string {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  const p = (n: number, l = 2) => String(n).padStart(l, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
export { fmtTs };
