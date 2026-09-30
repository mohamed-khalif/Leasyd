// Shared building blocks of the explorer pages: stacked bars over time, a duration heat map,
// a tree map, a pass/fail strip, a views sidebar, tabs, a column picker, collapsible sections
// and a side drawer. Plain SVG and CSS; sizes follow the container.
import { ReactNode, useEffect, useRef, useState } from "react";
import { fmtMs, fmtNum, fmtTime, Range } from "../time";

/** The element's width, kept current as it resizes. */
export function useWidth(initial = 600) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

export function niceMax(v: number): number {
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

const axis = { font: "10px var(--font-mono)" };

function Tip({ x, w, children }: { x: number; w: number; children: ReactNode }) {
  return <div className="chart-tip" style={{ left: Math.max(0, Math.min(x + 12, w - 210)) }}>{children}</div>;
}

// ------------------------------------------------------------------ stacked bars

export type StackKey = { label: string; color: string };
export type Bar = { t: number; values: number[] };    // t: bucket start (epoch ms); values in StackKey order

/** Bars over time, each split into coloured parts (e.g. log records by severity). */
export function StackedBars(p: { bars: Bar[]; keys: StackKey[]; range: Range; bucketMs: number; height?: number; unit?: string;
                                 legend?: boolean; onBar?: (t: number) => void }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const h = p.height ?? 150, padR = 6, padT = 6, padB = 20;
  const x1 = p.range.to ?? Date.now(), x0 = p.range.from ?? x1 - p.range.minutes * 60_000;
  const ymax = niceMax(Math.max(1, ...p.bars.map((b) => b.values.reduce((a, v) => a + v, 0))));
  const padL = Math.max(36, 10 + 6.2 * `${fmtNum(ymax)}${p.unit ?? ""}`.length);
  const X = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * (w - padL - padR);
  const Y = (v: number) => padT + (1 - v / ymax) * (h - padT - padB);
  const bw = Math.max(1, X(x0 + p.bucketMs) - X(x0) - 1);
  const hb = hover == null ? null : p.bars.find((b) => hover >= X(b.t) && hover <= X(b.t) + bw + 1);
  return (
    <div>
      {p.legend !== false && <div className="legend">{p.keys.map((k) => <span key={k.label}><i style={{ background: k.color }} />{k.label}</span>)}</div>}
      <div ref={ref} style={{ position: "relative" }}>
        <svg width={w} height={h} style={{ display: "block", cursor: p.onBar ? "pointer" : undefined }}
             onMouseMove={(e) => setHover(e.nativeEvent.offsetX)} onMouseLeave={() => setHover(null)}
             onClick={() => hb && p.onBar?.(hb.t)}>
          {[0, ymax / 2, ymax].map((v) => (
            <g key={v}>
              <line x1={padL} x2={w - padR} y1={Y(v)} y2={Y(v)} stroke="var(--border)" strokeDasharray={v ? "2 3" : undefined} />
              <text x={padL - 6} y={Y(v) + 3} textAnchor="end" fill="var(--text-3)" style={axis}>{fmtNum(v)}{p.unit ?? ""}</text>
            </g>
          ))}
          {Array.from({ length: 5 }, (_, i) => x0 + ((x1 - x0) * (i + 0.5)) / 5).map((t) => (
            <text key={t} x={X(t)} y={h - 5} textAnchor="middle" fill="var(--text-3)" style={axis}>{fmtTime(new Date(t).toISOString(), p.range)}</text>
          ))}
          {p.bars.map((b) => {
            let acc = 0;
            return (
              <g key={b.t} opacity={hb && hb !== b ? 0.55 : 1}>
                {b.values.map((v, i) => {
                  if (!v) return null;
                  const y0 = Y(acc), y1 = Y(acc + v);
                  acc += v;
                  const bx = Math.max(padL + 1, X(b.t));   // a bucket starting before the range: only its part inside
                  return <rect key={i} x={bx} y={y1} width={Math.max(1, bw - (bx - X(b.t)))} height={Math.max(0.5, y0 - y1)} fill={p.keys[i].color} />;
                })}
              </g>
            );
          })}
        </svg>
        {hb && (
          <Tip x={hover!} w={w}>
            <div className="faint">{new Date(hb.t).toLocaleString()}</div>
            {p.keys.map((k, i) => hb.values[i] ? (
              <div key={k.label} className="tip-row"><i style={{ background: k.color }} />{k.label}<b>{fmtNum(hb.values[i])}{p.unit ?? ""}</b></div>
            ) : null)}
          </Tip>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ heat map

export type Cell = { t: number; b: number; count: number; errors: number };   // b: log bucket (see bucketLow)
/** Buckets per doubling, as the engine's group_by "log:duration_ns". */
export const LOG_STEPS = 2;
export const bucketLow = (b: number) => 2 ** (b / LOG_STEPS);

/** Duration against time: each cell's shade is how many spans took that long then; red where they failed. */
export function Heatmap(p: { cells: Cell[]; range: Range; bucketMs: number; height?: number;
                             onCell?: (c: { t: number; b: number }) => void; selected?: { t: number; b: number } | null }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<Cell | null>(null);
  const [hx, setHx] = useState(0);
  const h = p.height ?? 180, padL = 60, padR = 6, padT = 4, padB = 20;
  const x1 = p.range.to ?? Date.now(), x0 = p.range.from ?? x1 - p.range.minutes * 60_000;
  const bs = p.cells.map((c) => c.b);
  const lo = bs.length ? Math.min(...bs) : 0, hi = bs.length ? Math.max(...bs) : 1;
  const rows = Math.max(1, hi - lo + 1), rh = (h - padT - padB) / rows;
  const X = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * (w - padL - padR);
  const cw = Math.max(1.5, X(x0 + p.bucketMs) - X(x0) - 0.5);
  const Yb = (b: number) => padT + (hi - b) * rh;
  const top = Math.max(1, ...p.cells.map((c) => c.count));
  const shade = (c: Cell) => 0.12 + 0.88 * Math.sqrt(c.count / top);
  const ticks = rows <= 6 ? Array.from({ length: rows }, (_, i) => lo + i) : Array.from({ length: 5 }, (_, i) => Math.round(lo + (i * (rows - 1)) / 4));
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <svg width={w} height={h} style={{ display: "block", cursor: p.onCell ? "crosshair" : undefined }} onMouseLeave={() => setHover(null)}>
        {ticks.map((b) => (
          <text key={b} x={padL - 6} y={Yb(b) + rh / 2 + 3} textAnchor="end" fill="var(--text-3)" style={axis}>{fmtMs(bucketLow(b))}</text>
        ))}
        {Array.from({ length: 5 }, (_, i) => x0 + ((x1 - x0) * (i + 0.5)) / 5).map((t) => (
          <text key={t} x={X(t)} y={h - 5} textAnchor="middle" fill="var(--text-3)" style={axis}>{fmtTime(new Date(t).toISOString(), p.range)}</text>
        ))}
        <line x1={padL} x2={w - padR} y1={h - padB} y2={h - padB} stroke="var(--border)" />
        {p.cells.map((c) => {
          const sel = p.selected && p.selected.t === c.t && p.selected.b === c.b;
          const x = Math.max(padL + 1, X(c.t)), cwx = Math.max(1, cw - (x - X(c.t))), ch = Math.max(1, rh - 1);
          const errShare = c.errors / c.count;      // red for the part of the cell's spans that failed
          return (
            <g key={`${c.t}:${c.b}`} onMouseMove={(e) => { setHover(c); setHx(e.nativeEvent.offsetX); }} onClick={() => p.onCell?.({ t: c.t, b: c.b })}>
              <rect x={x} y={Yb(c.b) + 0.5} width={cwx} height={ch} rx={1} fill="var(--accent)" opacity={shade(c)}
                    stroke={sel ? "var(--text)" : undefined} />
              {c.errors > 0 && <rect x={x} y={Yb(c.b) + 0.5} width={cwx} height={ch} rx={1} fill="var(--sev-error)" opacity={0.35 + 0.65 * Math.min(1, errShare * 2)} />}
            </g>
          );
        })}
      </svg>
      {hover && (
        <Tip x={hx} w={w}>
          <div className="faint">{new Date(hover.t).toLocaleString()}</div>
          <div className="tip-row">took {fmtMs(bucketLow(hover.b))} – {fmtMs(bucketLow(hover.b + 1))}</div>
          <div className="tip-row"><i style={{ background: "var(--accent)" }} />spans<b>{fmtNum(hover.count)}</b></div>
          {hover.errors > 0 && <div className="tip-row"><i style={{ background: "var(--sev-error)" }} />errors<b>{fmtNum(hover.errors)}</b></div>}
        </Tip>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ tree map

export type Tile = { label: string; value: number; sub?: string };
const TILE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];

/** Rectangles sized by value (squarified), largest first. */
export function Treemap(p: { tiles: Tile[]; height?: number; onTile?: (t: Tile) => void; fmt?: (v: number) => string }) {
  const [ref, w] = useWidth();
  const h = p.height ?? 220;
  const tiles = p.tiles.filter((t) => t.value > 0).sort((a, b) => b.value - a.value);
  const rects = squarify(tiles.map((t) => t.value), 0, 0, w, h);
  const fmt = p.fmt ?? fmtNum;
  return (
    <div ref={ref} className="treemap" style={{ height: h }}>
      {rects.map((r, i) => {
        const t = tiles[i];
        return (
          <button key={t.label} type="button" className="tile" title={`${t.label}: ${fmt(t.value)}${t.sub ? ` · ${t.sub}` : ""}`}
                  style={{ left: r.x, top: r.y, width: r.w, height: r.h, background: `color-mix(in srgb, ${TILE_COLORS[i % TILE_COLORS.length]} 30%, var(--surface-2))` }}
                  onClick={() => p.onTile?.(t)}>
            {r.w > 70 && r.h > 30 && <><span className="tile-label">{t.label}</span><span className="tile-value">{fmt(t.value)}</span></>}
          </button>
        );
      })}
    </div>
  );
}

type Rect = { x: number; y: number; w: number; h: number };
function squarify(values: number[], x: number, y: number, w: number, h: number): Rect[] {
  const total = values.reduce((a, v) => a + v, 0);
  if (!values.length || total <= 0 || w <= 0 || h <= 0) return [];
  const scale = (w * h) / total, areas = values.map((v) => v * scale), out: Rect[] = [];
  let i = 0;
  while (i < areas.length) {
    const side = Math.min(w, h);
    let row = [areas[i]], j = i + 1;
    const worst = (r: number[]) => {
      const s = r.reduce((a, v) => a + v, 0), mx = Math.max(...r), mn = Math.min(...r);
      return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
    };
    while (j < areas.length && worst([...row, areas[j]]) <= worst(row)) row = [...row, areas[j++]];
    const s = row.reduce((a, v) => a + v, 0), thick = s / side;
    let off = 0;
    for (const a of row) {
      const len = a / thick;
      out.push(w >= h ? { x, y: y + off, w: thick, h: len } : { x: x + off, y, w: len, h: thick });
      off += len;
    }
    if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
    i = j;
  }
  return out;
}

// ------------------------------------------------------------------ pass / fail strip

export type Block = { ok: boolean | null; excluded?: boolean; title: string; key?: string };

/** One block per run (or per period): green passed, red failed, grey none/excluded. */
export function StatusStrip(p: { blocks: Block[]; height?: number; onBlock?: (i: number) => void; selected?: string | null }) {
  return (
    <div className="strip" style={{ height: p.height ?? 26 }} role="list">
      {p.blocks.map((b, i) => (
        <span key={b.key ?? i} role="listitem" title={b.title} onClick={() => p.onBlock?.(i)}
              className={`strip-block ${b.ok == null ? "none" : b.ok ? "ok" : "fail"}${b.excluded ? " excluded" : ""}${p.selected && b.key === p.selected ? " sel" : ""}`}
              style={p.onBlock ? { cursor: "pointer" } : undefined} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ views, tabs, columns, sections, drawer

export type View = { key: string; label: string; hint?: string; group?: string };

/** Built-in views on the left of an explorer. */
export function ViewsSidebar(p: { views: View[]; active: string; onPick: (key: string) => void; title?: string }) {
  let group: string | undefined;
  return (
    <aside className="views" aria-label={p.title ?? "Views"}>
      <div className="views-title">{p.title ?? "Views"}</div>
      {p.views.map((v) => {
        const head = v.group && v.group !== group ? v.group : null;
        group = v.group;
        return (
          <div key={v.key}>
            {head && <div className="views-group">{head}</div>}
            <button type="button" className={v.key === p.active ? "on" : undefined} title={v.hint} onClick={() => p.onPick(v.key)}>{v.label}</button>
          </div>
        );
      })}
    </aside>
  );
}

export function Tabs<K extends string>(p: { tabs: [K, string][]; active: K; onPick: (k: K) => void; right?: ReactNode }) {
  return (
    <div className="tabs" role="tablist">
      {p.tabs.map(([k, label]) => (
        <button key={k} type="button" role="tab" aria-selected={k === p.active} className={k === p.active ? "on" : undefined} onClick={() => p.onPick(k)}>{label}</button>
      ))}
      <span className="spacer" />
      {p.right}
    </div>
  );
}

export type Column = { key: string; label: string };

/** A "Columns" button with a checklist of the table's columns. */
export function ColumnPicker(p: { columns: Column[]; shown: string[]; onChange: (shown: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("mousedown", close);
    return () => removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="menu" ref={ref}>
      <button type="button" className="btn small" onClick={() => setOpen(!open)} aria-expanded={open}>Columns ▾</button>
      {open && (
        <div className="menu-list columns-menu" style={{ right: 0, left: "auto" }}>
          <div className="faint" style={{ padding: "2px 6px 6px" }}>Show columns</div>
          {p.columns.map((c) => (
            <label key={c.key}>
              <input type="checkbox" checked={p.shown.includes(c.key)}
                     onChange={(e) => p.onChange(e.target.checked ? p.columns.map((x) => x.key).filter((k) => k === c.key || p.shown.includes(k))
                                                                  : p.shown.filter((k) => k !== c.key))} />
              {c.label}
            </label>
          ))}
          <button type="button" className="linkbtn" style={{ padding: "6px" }} onClick={() => p.onChange(p.columns.map((c) => c.key))}>Show all</button>
        </div>
      )}
    </div>
  );
}

/** Columns shown, remembered per table in this browser. */
export function useColumns(id: string, columns: Column[], initial?: string[]) {
  const all = columns.map((c) => c.key);
  const [shown, setShown] = useState<string[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(`leasyd.cols.${id}`) ?? "null");
      if (Array.isArray(saved)) return all.filter((k) => saved.includes(k));
    } catch { /* ignore */ }
    return initial ?? all;
  });
  const set = (s: string[]) => { setShown(s); try { localStorage.setItem(`leasyd.cols.${id}`, JSON.stringify(s)); } catch { /* ignore */ } };
  return [shown, set] as const;
}

/** A titled section that folds away (remembered in this browser). */
export function Section(p: { id: string; title: string; right?: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(() => { try { return localStorage.getItem(`leasyd.fold.${p.id}`) !== "1"; } catch { return true; } });
  const toggle = () => { setOpen(!open); try { localStorage.setItem(`leasyd.fold.${p.id}`, open ? "1" : "0"); } catch { /* ignore */ } };
  return (
    <section className="fold">
      <header className="fold-head">
        <button type="button" className="fold-toggle" onClick={toggle} aria-expanded={open}>
          <span className={`caret${open ? " open" : ""}`}>▸</span>{p.title}
        </button>
        <span className="spacer" />
        {p.right}
      </header>
      {open && <div className="fold-body">{p.children}</div>}
    </section>
  );
}

/** A panel sliding in from the right, over the page (Esc closes it). */
export function Drawer(p: { title: ReactNode; onClose: () => void; children: ReactNode; right?: ReactNode }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") p.onClose(); };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, [p]);
  return (
    <div className="drawer-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget) p.onClose(); }}>
      <aside className="drawer" role="dialog" aria-label={typeof p.title === "string" ? p.title : "Details"}>
        <header className="drawer-head">
          <div className="drawer-title">{p.title}</div>
          <span className="spacer" />
          {p.right}
          <button type="button" className="btn" onClick={p.onClose} aria-label="Close">✕</button>
        </header>
        <div className="drawer-body">{p.children}</div>
      </aside>
    </div>
  );
}

/** A big number with a label, for the rows of cards at the top of a page. */
export function Card(p: { label: string; value: ReactNode; sub?: ReactNode; tone?: "ok" | "bad" | "warn"; onClick?: () => void }) {
  return (
    <div className={`card${p.onClick ? " clickable" : ""}`} onClick={p.onClick}>
      <div className="card-label">{p.label}</div>
      <div className={`card-value${p.tone ? ` ${p.tone}` : ""}`}>{p.value}</div>
      {p.sub && <div className="card-sub">{p.sub}</div>}
    </div>
  );
}
