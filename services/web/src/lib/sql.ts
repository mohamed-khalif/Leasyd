// Building SQL text from values. The engine also checks every query (read-only, its own tables),
// but a value must still never end its string early.

/** A text value as a SQL string literal: 'it''s'. */
export const sqlString = (s: string) => `'${s.replace(/'/g, "''")}'`;
