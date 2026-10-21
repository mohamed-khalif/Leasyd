// A number with a sparkline under it (the service panels' requests, error rate and duration).
export function Tile({ label, value, sub, pts, bars, bad }: { label: string; value: string; sub: string; pts: number[]; bars?: boolean; bad?: boolean }) {
  const max = Math.max(1e-9, ...pts), W = 120, H = 34;
  const x = (i: number) => (pts.length > 1 ? (i / (pts.length - 1)) * W : W / 2);
  const y = (v: number) => H - (v / max) * (H - 2);
  return (
    <div className="svc-tile">
      <span className="faint">{label}</span>
      <b className={bad ? "bad" : undefined}>{value}</b>
      <span className="faint">{sub}</span>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {pts.length > 0 && (bars
          ? pts.map((v, i) => <rect key={i} className="spark-bar" x={(i * W) / pts.length} width={Math.max(1, W / pts.length - 1.5)} y={y(v)} height={H - y(v)} />)
          : <polyline className={`spark-line${bad ? " bad" : ""}`} points={pts.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />)}
      </svg>
    </div>
  );
}
