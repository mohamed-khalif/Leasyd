// CSV export for query results.

const cell = (v: unknown) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

/** Columns and rows -> CSV text. Text that a spreadsheet would run as a formula (starting with
 *  = + - @ tab or CR, e.g. from log data) gets a leading ' so it opens as plain text. Numbers are left as is. */
export function toCsv(columns: string[], rows: unknown[][]): string {
  const esc = (v: unknown) => {
    let s = cell(v);
    if (typeof v !== "number" && /^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [columns.map(esc).join(","), ...rows.map((row) => row.map(esc).join(","))].join("\n");
}

/** Save columns and rows as a CSV file. */
export function downloadCsv(name: string, columns: string[], rows: unknown[][]) {
  const a = document.createElement("a");
  const url = URL.createObjectURL(new Blob([toCsv(columns, rows)], { type: "text/csv" }));
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
