// List prices, as on the public pricing page (site/pricing.html and site/pricing.js): change them in
// all three places. Two parts, per million: ingest, on every record received (dropped ones too), and
// storage, on what is kept (30 days). Check runs per thousand.
export const PRICE = {
  traces: { ingest: 0.05, storage: 0.45 },
  logs: { ingest: 0.05, storage: 0.45 },
  metrics: { ingest: 0.015, storage: 0.15 },
} as const;
export const CHECK_PRICE_PER_THOUSAND = { http: 0.18, browser: 3.0 } as const;
export const PRICE_LABEL = { traces: "spans", logs: "log records", metrics: "metric data points" } as const;
export const usd = (v: number) => "US$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
