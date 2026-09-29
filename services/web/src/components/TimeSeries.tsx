import { useEffect, useRef, useState } from "react";
import { fmtNum, fmtTime, Range } from "../time";

export type Series = { label: string; color: string; points: [number, number][] }; // [epoch ms, value]

/** Line/area chart over time: y ticks, x time labels, legend, hover readout. */
export function TimeSeries(props: { series: Series[]; range: Range; height?: number; unit?: string; area?: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(600);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const h = props.height ?? 160, padL = 44, padR = 8, padT = 8, padB = 22;
  const xs = props.series.flatMap((s) => s.points.map((p) => p[0]));
  const now = Date.now();
  const x0 = Math.min(now - props.range.minutes * 60_000, ...(xs.length ? xs : [now]));
  const x1 = now;
  const top = Math.max(0, ...props.series.flatMap((s) => s.points.map((p) => p[1])));
  const ymax = niceMax(top > 0 ? top : 1);   // small values (ratios, rates) get their own scale
  const X = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * (w - padL - padR);
  const Y = (v: number) => padT + (1 - v / ymax) * (h - padT - padB);
  const ticksY = [0, ymax / 2, ymax];
  const ticksX = Array.from({ length: 5 }, (_, i) => x0 + ((x1 - x0) * (i + 0.5)) / 5);

  const hoverT = hover == null ? null : x0 + ((hover - padL) / (w - padL - padR)) * (x1 - x0);
  const nearest = (s: Series) => {
    if (hoverT == null || !s.points.length) return null;
    return s.points.reduce((a, b) => (Math.abs(b[0] - hoverT) < Math.abs(a[0] - hoverT) ? b : a));
  };

  return (
    <div>
      <div className="legend">{props.series.map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label}</span>)}</div>
      <div ref={box} style={{ position: "relative" }}>
        <svg width={w} height={h} style={{ display: "block" }}
             onMouseMove={(e) => setHover(e.nativeEvent.offsetX)} onMouseLeave={() => setHover(null)}>
          {ticksY.map((v) => (
            <g key={v}>
              <line x1={padL} x2={w - padR} y1={Y(v)} y2={Y(v)} stroke="var(--border)" strokeDasharray={v ? "2 3" : undefined} />
              <text x={padL - 6} y={Y(v) + 3} textAnchor="end" fill="var(--text-3)" style={{ font: "10px var(--font-mono)" }}>
                {fmtNum(v)}{props.unit ?? ""}
              </text>
            </g>
          ))}
          {ticksX.map((t) => (
            <text key={t} x={X(t)} y={h - 6} textAnchor="middle" fill="var(--text-3)" style={{ font: "10px var(--font-mono)" }}>
              {fmtTime(new Date(t).toISOString(), props.range)}
            </text>
          ))}
          {props.series.map((s) => {
            const pts = [...s.points].sort((a, b) => a[0] - b[0]);
            if (!pts.length) return null;
            const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
            return (
              <g key={s.label}>
                {props.area !== false && (
                  <path d={`${line} L${X(pts[pts.length - 1][0]).toFixed(1)},${Y(0)} L${X(pts[0][0]).toFixed(1)},${Y(0)} Z`}
                        fill={s.color} opacity={0.14} />
                )}
                <path d={line} fill="none" stroke={s.color} strokeWidth={1.4} />
              </g>
            );
          })}
          {hover != null && hover > padL && hover < w - padR && (
            <line x1={hover} x2={hover} y1={padT} y2={h - padB} stroke="var(--text-3)" strokeDasharray="3 3" />
          )}
        </svg>
        {hoverT != null && hover! > padL && hover! < w - padR && (
          <div style={{
            position: "absolute", top: 4, left: Math.min(hover! + 12, w - 190), pointerEvents: "none",
            background: "var(--surface)", border: "1px solid var(--border-strong)", borderRadius: 6,
            padding: "6px 9px", font: "11px var(--font-mono)", minWidth: 150, zIndex: 2,
          }}>
            <div className="faint" style={{ marginBottom: 3 }}>{fmtTime(new Date(hoverT).toISOString(), { ...props.range, minutes: 60 })}</div>
            {props.series.slice(0, 8).map((s) => {
              const p = nearest(s);
              return p && <div key={s.label}><i style={{ display: "inline-block", width: 8, height: 8, background: s.color, marginRight: 6, borderRadius: 2 }} />
                {s.label} <b style={{ float: "right", marginLeft: 12 }}>{fmtNum(p[1])}{props.unit ?? ""}</b></div>;
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function niceMax(v: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
