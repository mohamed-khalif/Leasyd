import { ReactNode, useState } from "react";
import { fmtNum } from "../lib/format";

/** Long lists draw their first PAGE_ROWS rows; "Show more" adds as many again. */
export const PAGE_ROWS = 500;

/** Renders children with at most `limit` rows (more on request) and says how many there are.
 *  Only drawing is capped: whatever searches, counts or exports works on `rows` itself. Give it
 *  a `key` that changes with the list's subject (a new trace) to start from the top again. */
export function Capped<T>({ rows, children }: { rows: T[]; children: (shown: T[]) => ReactNode }) {
  const [limit, setLimit] = useState(PAGE_ROWS);
  const shown = rows.length > limit ? rows.slice(0, limit) : rows;
  return (
    <>
      {children(shown)}
      {rows.length > shown.length && (
        <div className="panel-foot faint">
          Showing {fmtNum(shown.length)} of {fmtNum(rows.length)}
          <button type="button" className="btn small" onClick={() => setLimit((l) => l + PAGE_ROWS)}>Show more</button>
        </div>
      )}
    </>
  );
}
