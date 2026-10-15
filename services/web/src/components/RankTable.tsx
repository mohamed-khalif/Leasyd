import { ReactNode } from "react";

/** A ranked table: label columns, then a right-aligned value column (sorted by the caller). */
export function RankTable(props: { head: string[]; rows: ReactNode[][]; onRow?: (i: number) => void; maxHeight?: number; numCols?: number }) {
  const nums = props.numCols ?? 1;   // right-aligned value columns, counted from the end
  const isNum = (j: number, n: number) => j >= n - nums;
  return (
    <div className="table-scroll" style={props.maxHeight ? { maxHeight: props.maxHeight } : undefined}>
      <table className="table">
        <thead>
          <tr>{props.head.map((h, i) => (
            <th key={i} className={narrow(h) ? "narrow" : isNum(i, props.head.length) ? "num" : undefined}>{label(h)}</th>
          ))}</tr>
        </thead>
        <tbody>
          {props.rows.map((r, i) => (
            <tr key={i} onClick={props.onRow && (() => props.onRow!(i))} style={props.onRow ? { cursor: "pointer" } : undefined}>
              {r.map((c, j) => <td key={j} className={narrow(props.head[j] ?? "x") ? "narrow" : isNum(j, r.length) ? "num" : undefined} title={typeof c === "string" ? c : undefined}>{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const narrow = (h: string) => h === "" || h === "#";   // a status dot or a step number
// "send to" -> "Send to"; attribute names such as http.route stay as they are
const label = (h: string) => (/^[a-z][a-z &]*$/.test(h) ? h[0].toUpperCase() + h.slice(1) : h);
