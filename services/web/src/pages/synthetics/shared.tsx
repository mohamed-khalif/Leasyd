// What the synthetic-check pages share (the list and form in Synthetics.tsx, one check in
// CheckDetail.tsx): how steps, constraints and results read.
import { useState } from "react";
import { fmtFixed, fmtNum, fmtRatio } from "../../lib/format";
import { BrowserStep, CheckResult, Constraint, Step, StepResult } from "../../api";

export const OK = "var(--ok, #3fb68b)";
export const every = (f: number) => (f === 60 ? "1 hour" : `${f} min`);
export const isBrowser = (c: { type?: string }) => c.type === "browser";

/** One line for a step: "GET https://…" or "Type {password} into #password". */
export function describeStep(st: Step | BrowserStep): string {
  if (!("action" in st)) return `${st.method} ${st.url}`;
  const sel = st.selector || "…";
  st = { ...st, text: st.text ?? "", value: st.value ?? "", url: st.url ?? "", key: st.key || "…", variable: st.variable || "…" };
  switch (st.action) {
    case "navigate": return `Open ${st.url}`;
    case "click": return `Click ${sel}`;
    case "hover": return `Hover ${sel}`;
    case "type": return `Type “${st.text}” into ${sel}`;
    case "select": return `Select “${st.value}” in ${sel}`;
    case "press": return `Press ${st.key}${st.selector ? ` in ${st.selector}` : ""}`;
    case "wait_for": return `Wait for ${sel}`;
    case "wait": return `Wait ${st.ms} ms`;
    case "assert_text": return `Page shows “${st.text}”${st.selector ? ` in ${st.selector}` : ""}`;
    case "assert_no_text": return `Page doesn't show “${st.text}”`;
    case "assert_element": return `Element ${sel} is visible`;
    case "assert_url": return `URL contains “${st.value}”`;
    case "extract": return `Save ${st.attribute ? `${st.attribute} of ` : "text of "}${sel} as {${st.variable}}`;
  }
}

// Two decimals from 90% up, so 98.96% never shows as "99.0%" (and red, below a 99% target).
export const pct = (v: number) => fmtRatio(v);

export const OPS: Record<string, string> = { equals: "=", not_equals: "≠", contains: "contains", lt: "<", gt: ">", exists: "exists" };
export function describeConstraint(c: Constraint): string {
  switch (c.type) {
    case "status": return `status ${c.expr}`;
    case "max_ms": return `≤ ${c.value} ms`;
    case "body_contains": return `body has “${c.value}”`;
    case "body_not_contains": return `body lacks “${c.value}”`;
    case "body_regex": return `body ~ /${c.value}/`;
    case "header": return `${c.name} ${OPS[c.op ?? "exists"]}${c.op !== "exists" ? ` “${c.value}”` : ""}`;
    case "json": return `${c.path} ${OPS[c.op ?? "exists"]}${c.op !== "exists" ? ` ${c.value}` : ""}`;
    case "tls_days": return `cert ≥ ${c.value} days`;
    default: return c.type;
  }
}

export function ResultBox({ result, note }: { result: CheckResult; note: string }) {
  return (
    <div className={`result-box ${result.ok ? "pass" : "fail"}`} role="status">
      <b>{result.ok ? "Passed" : "Failed"}</b> <span className="faint">· {fmtNum(result.total_ms)} ms{result.tls_days != null ? ` · certificate valid for ${Math.floor(result.tls_days)} more days` : ""}</span>
      {!result.steps.length && result.failure && <div>{result.failure}</div>}
      <ol className="result-steps">
        {result.steps.map((s, i) => {
          const t = s.timings, parts: [string, number | undefined][] = [["DNS", t.dns_ms], ["connect", t.connect_ms], ["TLS", t.tls_ms], ["first byte", t.ttfb_ms], ["total", t.total_ms]];
          return (
            <li key={i}>
              <span style={{ color: s.ok ? OK : "var(--sev-error)" }}>{s.ok ? "✓" : "✕"}</span> <b>{s.name}</b>
              {s.status != null && <span className="mono"> · HTTP {s.status}</span>}
              {s.failure && <span> · {s.failure}</span>}
              {s.extracted.length > 0 && <span className="faint"> · set {s.extracted.map((x) => `{${x}}`).join(" ")}</span>}
              <div className="faint mono">{parts.filter(([, v]) => v != null).map(([k, v]) => `${k} ${fmtNum(v!)} ms`).join(" · ")}{s.vitals && ` · ${describeVitals(s)}`}</div>
              {s.body_sample && <pre className="body-sample">{s.body_sample}</pre>}
              <BrowserDetails s={s} />
            </li>
          );
        })}
      </ol>
      <div className="faint">{note}</div>
    </div>
  );
}

function describeVitals(s: StepResult): string {
  const v = s.vitals!, ms = (x: number | null) => (x == null ? null : `${fmtNum(x)} ms`);
  return [["first byte", ms(v.ttfb_ms)], ["first paint", ms(v.fcp_ms)], ["largest paint", ms(v.lcp_ms)], ["loaded", ms(v.load_ms)],
          ["layout shift", v.cls == null ? null : fmtFixed(v.cls, 3)]].filter(([, x]) => x != null).map(([k, x]) => `${k} ${x}`).join(" · ");
}

function BrowserDetails({ s }: { s: StepResult }) {
  const [big, setBig] = useState(false);
  const lists: [string, string[] | undefined][] = [["Console errors", s.console_errors], ["Responses with errors", s.http_errors],
                                                   ["Failed requests", s.failed_requests], ["Blocked (not a public address)", s.blocked]];
  return (<>
    {lists.filter(([, l]) => l && l.length).map(([title, l]) => (
      <div key={title} className="faint"><b>{title}:</b> <span className="mono">{l!.join(" · ")}</span></div>
    ))}
    {s.screenshot && <img className={`shot${big ? "" : " small"}`} src={`data:image/jpeg;base64,${s.screenshot}`} alt={`Screenshot after ${s.name}`}
                          title={big ? "Click to shrink" : "Click to enlarge"} onClick={() => setBig(!big)} />}
  </>);
}
