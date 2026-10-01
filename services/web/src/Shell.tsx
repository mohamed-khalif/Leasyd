import { ReactNode, useEffect, useRef, useState } from "react";
import { IconClock, IconCost, IconDash, IconLogo, IconLogs, IconMetrics, IconMoon, IconOut, IconPulse, IconRefresh, IconSun, IconTarget, IconTraces, IconBell, IconFlask, IconDb, IconGear, IconSpark } from "./icons";
import { customRange, fmtDay, keptFrom, MAX_CUSTOM_DAYS, RANGES, Range, rangeWindow, RETENTION_DAYS } from "./time";

type Props = {
  path: string; crumb: [string, string]; user: { tenant: string; email: string }; range: Range;
  onRange: (r: Range) => void; onRefresh: () => void; onSignOut: () => void; children: ReactNode;
};

export function Shell(p: Props) {
  const [light, setLight] = useState(document.documentElement.dataset.theme === "light");
  const toggleTheme = () => {
    const next = !light;
    setLight(next);
    if (next) document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("leasyd.theme", next ? "light" : "dark"); } catch { /* ignore */ }
  };
  const link = (href: string, label: string, icon: ReactNode, active: boolean) => (
    <a href={`#${href}`} className={active ? "active" : undefined}>{icon}{label}</a>
  );
  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <div className="brand"><IconLogo />Leasyd</div>
        <a href="#/ai" className={`ask-ai${p.path.startsWith("/ai") ? " active" : ""}`}><IconSpark />Ask Leasyd AI</a>
        <div className="nav-section">Dashboards</div>
        {link("/", "Home", <IconDash />, p.path === "/" || p.path === "")}
        {link("/dashboards", "Dashboards", <IconDash />, p.path.startsWith("/dashboards"))}
        {link("/usage", "Usage & Cost", <IconCost />, p.path.startsWith("/usage"))}
        <div className="nav-section">Explore</div>
        {link("/logs", "Logs", <IconLogs />, p.path.startsWith("/logs"))}
        {link("/traces", "Traces", <IconTraces />, p.path.startsWith("/traces"))}
        {link("/metrics", "Metrics", <IconMetrics />, p.path.startsWith("/metrics"))}
        <div className="nav-section">Query data</div>
        {link("/query", "Query Builder", <IconFlask />, p.path.startsWith("/query"))}
        {link("/sql", "SQL", <IconDb />, p.path.startsWith("/sql"))}
        <div className="nav-section">Monitoring</div>
        {link("/synthetics", "Synthetics", <IconPulse />, p.path.startsWith("/synthetics"))}
        {link("/slos", "SLOs", <IconTarget />, p.path.startsWith("/slos"))}
        {link("/alerts", "Alerts", <IconBell />, p.path.startsWith("/alerts"))}
        <div className="nav-section">Account</div>
        {link("/settings", "Settings", <IconGear />, p.path.startsWith("/settings"))}
        <div className="nav-foot">
          <div className="whoami" title={p.user.email}>{p.user.email}<div className="faint mono">{p.user.tenant}</div></div>
          <button className="btn ghost" onClick={p.onSignOut}><IconOut />Sign out</button>
        </div>
      </nav>
      <div className="main">
        <header className="topbar">
          <div className="crumb"><span className="muted">{p.crumb[0]}: </span><b>{p.crumb[1]}</b></div>
          <span className="spacer" />
          <button className="btn" onClick={toggleTheme} title={light ? "Dark theme" : "Light theme"} aria-label="Toggle theme">
            {light ? <IconMoon /> : <IconSun />}
          </button>
          <RangePicker range={p.range} onRange={p.onRange} />
          <button className="btn" onClick={p.onRefresh} title="Refresh" aria-label="Refresh"><IconRefresh /></button>
        </header>
        <div className="content">{p.children}</div>
      </div>
    </div>
  );
}

function RangePicker(p: { range: Range; onRange: (r: Range) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("mousedown", close);
    return () => removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="menu" ref={ref}>
      <button className="btn" onClick={() => setOpen(!open)} aria-haspopup="listbox" aria-expanded={open}>
        <IconClock />{p.range.label}<span className="faint">▾</span>
      </button>
      {open && (
        <div className="menu-list range-menu">
          <div role="listbox" aria-label="Quick ranges">
            {RANGES.map((r) => (
              <button key={r.key} role="option" aria-selected={r.key === p.range.key} className={r.key === p.range.key ? "on" : undefined}
                      onClick={() => { p.onRange(r); setOpen(false); }}>{r.label}</button>
            ))}
          </div>
          <CustomRange range={p.range} onRange={(r) => { p.onRange(r); setOpen(false); }} />
        </div>
      )}
    </div>
  );
}

/** "YYYY-MM-DDTHH:mm" in local time, as <input type="datetime-local"> wants it. */
const toInput = (ms: number) => {
  const d = new Date(ms), p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};

/** A fixed from/to, with calendar pickers and quick days. */
function CustomRange(p: { range: Range; onRange: (r: Range) => void }) {
  const w = rangeWindow(p.range);
  const [from, setFrom] = useState(toInput(Date.parse(w.start)));
  const [to, setTo] = useState(toInput(Date.parse(w.end)));
  const [error, setError] = useState<string | null>(null);
  const apply = (f: number, t: number) => {
    if (!(f < t)) return setError("“From” must be before “To”.");
    if (t - f > MAX_CUSTOM_DAYS * 86_400_000) return setError(`Choose at most ${MAX_CUSTOM_DAYS} days.`);
    if (f < keptFrom()) return setError(`Data is kept for ${RETENTION_DAYS} days: choose ${fmtDay(keptFrom())} or later.`);
    p.onRange(customRange(f, Math.min(t, Date.now())));
  };
  const day = (offset: number) => {          // a whole local day: 0 = today (until now), -1 = yesterday
    const start = new Date(); start.setHours(0, 0, 0, 0); start.setDate(start.getDate() + offset);
    const end = new Date(start); end.setDate(end.getDate() + 1);
    apply(start.getTime(), Math.min(end.getTime(), Date.now()));
  };
  return (
    <form className="range-custom" onSubmit={(e) => { e.preventDefault(); setError(null); apply(new Date(from).getTime(), new Date(to).getTime()); }}>
      <div className="faint">Custom range</div>
      <label>From<input className="input" type="datetime-local" value={from} min={toInput(keptFrom())} max={toInput(Date.now())} required
                        onChange={(e) => setFrom(e.target.value)} /></label>
      <label>To<input className="input" type="datetime-local" value={to} min={toInput(keptFrom())} max={toInput(Date.now())} required
                      onChange={(e) => setTo(e.target.value)} /></label>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="faint">Data is kept for {RETENTION_DAYS} days.</div>
      <div className="range-custom-row">
        <button type="button" className="btn" onClick={() => day(0)}>Today</button>
        <button type="button" className="btn" onClick={() => day(-1)}>Yesterday</button>
        <span className="spacer" />
        <button className="btn primary">Apply</button>
      </div>
    </form>
  );
}
