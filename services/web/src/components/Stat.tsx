export function Stat(props: { value: string; sub?: string; small?: boolean }) {
  return (
    <div>
      <div className="stat-well">
        <span className={`stat-value${props.small ? " sm" : ""}`}>{props.value}</span>
      </div>
      {props.sub && <div className="stat-sub">{props.sub}</div>}
    </div>
  );
}

/** A half-circle gauge (value against a maximum) with a label under it. */
export function Gauge(props: { value: number; max: number; label: string; text: string; color: string }) {
  const r = 70, cx = 90, cy = 88, frac = Math.max(0, Math.min(1, props.max ? props.value / props.max : 0));
  const arc = (f: number) => {
    const a = Math.PI * (1 - f);
    return `${cx + r * Math.cos(a)},${cy - r * Math.sin(a)}`;
  };
  return (
    <svg viewBox="0 0 180 120" style={{ width: "100%", maxWidth: 240, display: "block", margin: "0 auto" }} role="img"
         aria-label={`${props.label}: ${props.text}`}>
      <path d={`M ${arc(0)} A ${r} ${r} 0 0 1 ${arc(1)}`} stroke="var(--surface-3)" strokeWidth="11" fill="none" strokeLinecap="round" />
      {frac > 0 && <path d={`M ${arc(0)} A ${r} ${r} 0 0 1 ${arc(frac)}`} stroke={props.color} strokeWidth="11" fill="none" strokeLinecap="round" />}
      <text x={cx} y={cy - 12} textAnchor="middle" fill={props.color} style={{ font: "500 22px var(--font-mono)" }}>{props.text}</text>
      <text x={cx} y={cy + 20} textAnchor="middle" fill="var(--text-2)" style={{ font: "11px var(--font-sans)" }}>{props.label}</text>
    </svg>
  );
}
