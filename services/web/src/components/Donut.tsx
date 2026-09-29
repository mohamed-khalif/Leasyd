import { fmtNum } from "../time";

export type Slice = { label: string; value: number; color: string };

export function Donut(props: { slices: Slice[] }) {
  const total = props.slices.reduce((a, s) => a + s.value, 0) || 1;
  const R = 62, r = 38, c = 80;
  let angle = -Math.PI / 2;
  const paths = props.slices.filter((s) => s.value > 0).map((s) => {
    const a0 = angle, a1 = angle + (2 * Math.PI * s.value) / total;
    angle = a1;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (rad: number, a: number) => `${c + rad * Math.cos(a)},${c + rad * Math.sin(a)}`;
    const d = a1 - a0 >= 2 * Math.PI - 1e-6
      ? `M ${c - R},${c} A ${R} ${R} 0 1 1 ${c + R},${c} A ${R} ${R} 0 1 1 ${c - R},${c} M ${c - r},${c} A ${r} ${r} 0 1 0 ${c + r},${c} A ${r} ${r} 0 1 0 ${c - r},${c} Z`
      : `M ${p(R, a0)} A ${R} ${R} 0 ${large} 1 ${p(R, a1)} L ${p(r, a1)} A ${r} ${r} 0 ${large} 0 ${p(r, a0)} Z`;
    return <path key={s.label} d={d} fill={s.color} fillRule="evenodd"><title>{`${s.label}: ${fmtNum(s.value)} (${((100 * s.value) / total).toFixed(1)}%)`}</title></path>;
  });
  return (
    <div>
      <div className="legend">{props.slices.map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label}</span>)}</div>
      <svg viewBox="0 0 160 160" style={{ width: "100%", maxWidth: 200, display: "block", margin: "0 auto" }} role="img">
        {paths}
        <text x={c} y={c + 5} textAnchor="middle" fill="var(--text)" style={{ font: "500 15px var(--font-mono)" }}>{fmtNum(total)}</text>
      </svg>
    </div>
  );
}
