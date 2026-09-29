import { ReactNode } from "react";

/** A ranked table: label columns, then a right-aligned value column (sorted by the caller). */
export function RankTable(props: { head: string[]; rows: ReactNode[][]; onRow?: (i: number) => void; maxHeight?: number; numCols?: number }) {
  const nums = props.numCols ?? 1;   // right-aligned value columns, counted from the end
  const isNum = (j: number, n: number) => j >= n - nums;
  return (
    <div className="table-scroll" style={props.maxHeight ? { maxHeight: props.maxHeight } : undefined}>
      <table className="table">
        <thead>
          <tr>{props.head.map((h, i) => <th key={i} className={isNum(i, props.head.length) ? "num" : undefined}>{h}{i === props.head.length - 1 ? " ↓" : ""}</th>)}</tr>
        </thead>
        <tbody>
          {props.rows.map((r, i) => (
            <tr key={i} onClick={props.onRow && (() => props.onRow!(i))} style={props.onRow ? { cursor: "pointer" } : undefined}>
              {r.map((c, j) => <td key={j} className={isNum(j, r.length) ? "num" : undefined} title={typeof c === "string" ? c : undefined}>{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
