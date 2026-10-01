// List prices per million records, as on the public pricing page (site/pricing.html).
// Change them in both places; the Usage & Cost dashboard reads nothing else.
export const PRICE_PER_MILLION = { traces: 0.3, logs: 0.3, metrics: 0.1 } as const;
export const PRICE_LABEL = { traces: "spans", logs: "log records", metrics: "metric data points" } as const;
export const usd = (v: number) => "US$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
