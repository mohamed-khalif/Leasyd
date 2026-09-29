import { ReactNode, useEffect, useRef, useState } from "react";
import { IconClock, IconCost, IconDash, IconLogo, IconLogs, IconMoon, IconOut, IconRefresh, IconSun, IconTraces } from "./icons";
import { RANGES, Range } from "./time";

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
        <div className="nav-section">Dashboards</div>
        {link("/", "Telemetry Insights", <IconDash />, p.path === "/" || p.path === "")}
        {link("/usage", "Usage & Cost", <IconCost />, p.path.startsWith("/usage"))}
        <div className="nav-section">Explore</div>
        {link("/logs", "Logs", <IconLogs />, p.path.startsWith("/logs"))}
        {link("/traces", "Traces", <IconTraces />, p.path.startsWith("/traces"))}
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
        <div className="menu-list" role="listbox">
          {RANGES.map((r) => (
            <button key={r.key} className={r.key === p.range.key ? "on" : undefined}
                    onClick={() => { p.onRange(r); setOpen(false); }}>{r.label}</button>
          ))}
        </div>
      )}
    </div>
  );
}
