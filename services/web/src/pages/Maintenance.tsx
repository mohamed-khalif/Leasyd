import { FormEvent, useEffect, useState } from "react";
import { Check, checks, MaintenanceWindow, windows, WindowSchedule } from "../api";
import type { Ctx } from "../App";
import { Panel } from "../components/Panel";
import { RankTable } from "../components/RankTable";

// Maintenance windows: while one is open, the chosen checks keep running but their runs are marked
// excluded, so a deployment doesn't count against uptime or SLOs.

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES: Record<string, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

export function Maintenance({ ctx, sub }: { ctx: Ctx; sub?: string }) {
  if (sub === "new") return <WindowForm ctx={ctx} />;
  if (sub) return <EditWindow ctx={ctx} id={sub} />;
  return <WindowList ctx={ctx} />;
}

function useChecks() {
  const [list, setList] = useState<Check[]>([]);
  useEffect(() => { checks.list().then((d) => setList(d.checks), () => setList([])); }, []);
  return list;
}

/** "Every Tue, Thu 22:00 for 1 h (Europe/London)" or "1 Oct 22:00 – 23:30". */
export function describeSchedule(s: WindowSchedule): string {
  if (s.type === "once") {
    const f = (t: string) => new Date(t).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    return `${f(s.start)} – ${f(s.end)}`;
  }
  const h = Math.floor(s.duration_minutes / 60), m = s.duration_minutes % 60;
  const len = [h ? `${h} h` : "", m ? `${m} min` : ""].filter(Boolean).join(" ");
  const days = s.days.length === 7 ? "day" : s.days.map((d) => DAY_NAMES[d]).join(", ");
  return `Every ${days} ${s.start} for ${len} (${s.timezone})`;
}

/** Whether a window is open now (as the backend decides; for display). */
export function isOpen(s: WindowSchedule, now = new Date()): boolean {
  if (s.type === "once") return Date.parse(s.start) <= now.getTime() && now.getTime() < Date.parse(s.end);
  // The local weekday and minute of the day in the window's time zone, today and yesterday.
  const parts = (t: Date) => {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: s.timezone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(t).map((x) => [x.type, x.value]));
    return { day: String(p.weekday).toLowerCase().slice(0, 3), minute: Number(p.hour) * 60 + Number(p.minute) };
  };
  const [hh, mm] = s.start.split(":").map(Number), opens = hh * 60 + mm;
  const today = parts(now), yesterday = parts(new Date(now.getTime() - 86_400_000));
  if (s.days.includes(today.day) && today.minute >= opens && today.minute < opens + s.duration_minutes) return true;
  return s.days.includes(yesterday.day) && today.minute + 1440 < opens + s.duration_minutes;
}

function WindowList({ ctx }: { ctx: Ctx }) {
  const [items, setItems] = useState<MaintenanceWindow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const all = useChecks();
  useEffect(() => { windows.list().then((d) => setItems(d.items), (e: Error) => setError(e.message)); }, [ctx.tick]);
  const names = new Map(all.map((c) => [c.id, c.name]));
  return (
    <>
      <div className="toolbar">
        <a href="#/synthetics" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/synthetics"); }}>← All checks</a>
        <span className="faint">While a window is open, its checks still run, but their results don't count against uptime or SLOs.</span>
        <span className="spacer" style={{ flex: 1 }} />
        <button className="btn primary" onClick={() => ctx.go("/synthetics/windows/new")}>New window</button>
      </div>
      <Panel title="Maintenance windows" flush>
        {error ? <div className="state error">{error}</div>
          : !items ? <div className="skeleton" style={{ height: 120 }} />
          : !items.length ? (
            <div className="state" style={{ minHeight: 160, flexDirection: "column", gap: 10 }}>
              <div>No maintenance windows. Add one for your regular deployment slot, or just before a release, so checks failing during it aren't counted.</div>
              <button className="btn primary" onClick={() => ctx.go("/synthetics/windows/new")}>Add a window</button>
            </div>
          ) : (
            <RankTable head={["window", "when", "checks", "now"]} maxHeight={600} onRow={(i) => ctx.go(`/synthetics/windows/${items[i].id}`)}
                       rows={items.map((w) => [<span className="link">{w.name} ›</span>, describeSchedule(w.schedule),
                                               w.checks[0] === "*" ? "all checks" : w.checks.map((id) => names.get(id) ?? "(deleted check)").join(", "),
                                               isOpen(w.schedule) ? <b>open</b> : <span className="faint">closed</span>])} />
          )}
      </Panel>
    </>
  );
}

function EditWindow({ ctx, id }: { ctx: Ctx; id: string }) {
  const [w, setW] = useState<MaintenanceWindow | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { windows.get(id).then(setW, (e: Error) => setError(e.message)); }, [id]);
  if (error) return <div className="state error">{error}</div>;
  return w ? <WindowForm ctx={ctx} existing={w} /> : <div className="skeleton" style={{ height: 240 }} />;
}

/** <input type="datetime-local"> value (local time) <-> ISO UTC. */
const toLocal = (iso: string) => { const d = new Date(iso); return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16); };
const toUtc = (local: string) => new Date(local).toISOString();

function WindowForm({ ctx, existing }: { ctx: Ctx; existing?: MaintenanceWindow }) {
  const all = useChecks();
  const inHour = new Date(Math.ceil(Date.now() / 3_600_000) * 3_600_000);
  const [name, setName] = useState(existing?.name ?? "");
  const [scope, setScope] = useState<string[]>(existing?.checks ?? ["*"]);
  const [kind, setKind] = useState<"once" | "weekly">(existing?.schedule.type ?? "once");
  const once = existing?.schedule.type === "once" ? existing.schedule : null;
  const weekly = existing?.schedule.type === "weekly" ? existing.schedule : null;
  const [start, setStart] = useState(toLocal(once?.start ?? inHour.toISOString()));
  const [end, setEnd] = useState(toLocal(once?.end ?? new Date(inHour.getTime() + 3_600_000).toISOString()));
  const [days, setDays] = useState<string[]>(weekly?.days ?? ["tue"]);
  const [time, setTime] = useState(weekly?.start ?? "22:00");
  const [minutes, setMinutes] = useState(weekly?.duration_minutes ?? 60);
  const [tz, setTz] = useState(weekly?.timezone ?? LOCAL_TZ);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const zones = [...new Set([tz, "UTC", LOCAL_TZ, ...((Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [])])];

  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    const schedule: WindowSchedule = kind === "once" ? { type: "once", start: toUtc(start), end: toUtc(end) }
      : { type: "weekly", days, start: time, duration_minutes: minutes, timezone: tz };
    try {
      if (existing) await windows.update(existing.id, { name, checks: scope, schedule });
      else await windows.create({ name, checks: scope, schedule });
      ctx.go("/synthetics/windows");
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!existing || !confirm(`Delete the window “${existing.name}”? Runs it already covered stay excluded.`)) return;
    try { await windows.remove(existing.id); ctx.go("/synthetics/windows"); } catch (err) { setError((err as Error).message); }
  };
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <form onSubmit={submit}>
      <div className="toolbar">
        <a href="#/synthetics/windows" className="btn" onClick={(e) => { e.preventDefault(); ctx.go("/synthetics/windows"); }}>← Cancel</a>
        <span className="spacer" style={{ flex: 1 }} />
        {existing && <button type="button" className="btn" onClick={remove}>Delete</button>}
      </div>
      <Panel title={existing ? `Edit “${existing.name}”` : "New maintenance window"}>
        <section className="form-section">
          <div className="form-grid">
            <label>Name<input className="input" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="Weekly deployment" /></label>
            <label>When
              <select className="select" value={kind} onChange={(e) => setKind(e.target.value as "once" | "weekly")}>
                <option value="once">Once</option><option value="weekly">Every week</option>
              </select>
            </label>
          </div>
          {kind === "once" ? (
            <div className="form-grid">
              <label>From (your time)<input className="input" type="datetime-local" required value={start} onChange={(e) => setStart(e.target.value)} /></label>
              <label>Until<input className="input" type="datetime-local" required value={end} onChange={(e) => setEnd(e.target.value)} /></label>
            </div>
          ) : (<>
            <div className="chips" style={{ margin: "8px 0" }}>
              {DAYS.map((d) => <button key={d} type="button" className={`chip${days.includes(d) ? " on" : ""}`} onClick={() => setDays(toggle(days, d))}>{DAY_NAMES[d]}</button>)}
            </div>
            <div className="form-grid">
              <label>Starts at<input className="input" type="time" required value={time} onChange={(e) => setTime(e.target.value)} /></label>
              <label>For (minutes)<input className="input" type="number" min={1} max={1440} required value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} /></label>
              <label>Time zone
                <select className="select" value={tz} onChange={(e) => setTz(e.target.value)}>{zones.map((z) => <option key={z}>{z}</option>)}</select>
              </label>
            </div>
          </>)}
        </section>
        <section className="form-section">
          <h3>Checks</h3>
          <div className="checks">
            <label className="check"><input type="checkbox" checked={scope[0] === "*"} onChange={(e) => setScope(e.target.checked ? ["*"] : [])} /> All checks (including ones added later)</label>
          </div>
          {scope[0] !== "*" && (
            <div className="checks">
              {all.map((c) => <label key={c.id} className="check"><input type="checkbox" checked={scope.includes(c.id)} onChange={() => setScope(toggle(scope, c.id))} /> {c.name}</label>)}
            </div>
          )}
        </section>
        {error && <div className="form-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}
        <div className="toolbar" style={{ marginTop: 14 }}>
          <button className="btn primary" disabled={busy}>{busy ? "Saving…" : existing ? "Save" : "Create window"}</button>
        </div>
      </Panel>
    </form>
  );
}
