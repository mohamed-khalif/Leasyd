// Placeholder list prices per million records, until real pricing is set.
// Change them here; the Usage & Cost dashboard reads nothing else.
export const PRICE_PER_MILLION = { traces: 0.2, logs: 0.2, metrics: 0.05 } as const;
export const PRICE_LABEL = { traces: "spans", logs: "log records", metrics: "metric data points" } as const;
export const usd = (v: number) => "US$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
