// What the GPU pages conclude from the query rows: spend, waste, recommendations, the demand trend.
// Pure functions (tested in analysis.test.ts); prices from model.ts or the account's own.
import { GpuPrice, IDLE_UTIL, priceFor, xidInfo } from "./model";
import type { GpuDayRow, GpuHealthRow, GpuRow, GpuWorkloadRow, StalledRow } from "./queries";

const HOURS_PER_MONTH = 730;

export type Spend = { total: number; active: number; idle: number; free: number };

/** Spend over the range per GPU row: its hours at its model's price, split by how they were used. */
export function fleetSpend(gpus: GpuRow[], prices: GpuPrice[] = []): Spend {
  const out = { total: 0, active: 0, idle: 0, free: 0 };
  for (const g of gpus) {
    const p = priceFor(g.model, prices).usd;
    out.total += g.hours * p;
    out.active += g.active_hours * p;
    out.idle += g.idle_hours * p;
    out.free += g.free_hours * p;
  }
  return out;
}

export type ModelSummary = { model: string; label: string; gpus: number; nodes: number; util: number; usd_per_hour: number; own_price: boolean };

/** The fleet by GPU model: how many, on how many nodes, how busy, at what price. */
export function byModel(gpus: GpuRow[], prices: GpuPrice[] = []): ModelSummary[] {
  const m = new Map<string, GpuRow[]>();
  for (const g of gpus) m.set(g.model, [...(m.get(g.model) ?? []), g]);
  return [...m.entries()].map(([model, gs]) => {
    const p = priceFor(model, prices);
    return { model, label: p.label, gpus: gs.length, nodes: new Set(gs.map((g) => g.node)).size,
             util: weighted(gs.map((g) => [g.util_avg, g.hours])), usd_per_hour: p.usd, own_price: p.own };
  }).sort((a, b) => b.gpus - a.gpus || a.model.localeCompare(b.model));
}

export type Now = { active: number; idle: number; free: number };

/** What the GPUs are doing at the end of the range. */
export function now(gpus: GpuRow[]): Now {
  const out = { active: 0, idle: 0, free: 0 };
  for (const g of gpus) {
    if ((g.util_now ?? 0) >= IDLE_UTIL) out.active++;
    else if (g.holder_now) out.idle++;
    else out.free++;
  }
  return out;
}

export type TeamRow = { team: string; gpus: number; workloads: number; hours: number; spend: number; idle_spend: number; util: number };

/** Spend by team (pod label team; else the namespace). */
export function byTeam(rows: GpuWorkloadRow[], prices: GpuPrice[] = []): TeamRow[] {
  const m = new Map<string, TeamRow & { _u: [number, number][] }>();
  for (const r of rows) {
    const team = r.team || `${r.namespace} (namespace)`;
    const p = priceFor(r.model, prices).usd;
    const t = m.get(team) ?? { team, gpus: 0, workloads: 0, hours: 0, spend: 0, idle_spend: 0, util: 0, _u: [] };
    t.gpus += r.gpus; t.workloads += 1; t.hours += r.hours; t.spend += r.hours * p; t.idle_spend += r.idle_hours * p;
    t._u.push([r.util_avg, r.hours]);
    m.set(team, t);
  }
  return [...m.values()].map(({ _u, ...t }) => ({ ...t, util: weighted(_u) })).sort((a, b) => b.spend - a.spend);
}

export type Recommendation = { level: "critical" | "warning" | "info"; title: string; detail: string; monthly_saving?: number; target?: string };

/** What to do about the GPUs, most valuable first: reclaim idle GPUs, give jobs waiting for GPUs
 *  the idle or spare ones, shrink spare capacity, fix hardware. */
export function recommendations(p: { gpus: GpuRow[]; workloads: GpuWorkloadRow[]; stalled: StalledRow[]; health: GpuHealthRow[];
  rangeHours: number; prices?: GpuPrice[] }): Recommendation[] {
  const prices = p.prices ?? [], out: Recommendation[] = [];
  if (p.rangeHours <= 0) return out;
  const perMonth = (usdInRange: number) => (usdInRange / p.rangeHours) * HOURS_PER_MONTH;
  for (const h of p.health) {
    for (const x of h.xids ?? []) {
      const info = xidInfo(x);
      if (info.level === "info") continue;
      out.push({ level: info.level, title: `XID ${x} on ${h.node} (${h.model}): ${info.title}`, detail: info.action, target: h.gpu });
    }
    if ((h.remap_failure ?? 0) > 0)
      out.push({ level: "critical", title: `Row remapping failed on ${h.node} (${h.model})`, detail: "The GPU can't remap bad memory any more: take it out of service and replace it.", target: h.gpu });
    else if ((h.dbe ?? 0) > 0 && !(h.xids ?? []).length)
      out.push({ level: "critical", title: `${h.dbe} double-bit ECC error${h.dbe === 1 ? "" : "s"} on ${h.node} (${h.model})`,
                 detail: "Uncorrectable memory errors: drain the node and reset the GPU; replace it if they come back.", target: h.gpu });
  }
  const waiting = p.stalled.filter((s) => s.phase === 1 || s.phase == null);
  const idle = p.workloads.filter((w) => w.hours > 0 && w.idle_hours / w.hours >= 0.8 && w.holding_now > 0)
    .map((w) => ({ w, usd: w.idle_hours * priceFor(w.model, prices).usd })).sort((a, b) => b.usd - a.usd);
  for (const { w, usd } of idle) {
    out.push({ level: "warning", title: `${w.workload} (${w.namespace}) holds ${w.holding_now} GPU${w.holding_now === 1 ? "" : "s"} it barely uses`,
               detail: `${Math.round((w.idle_hours / w.hours) * 100)}% of its GPU time was idle (under ${IDLE_UTIL}% utilization)` +
                 (w.team ? `; team ${w.team}` : "") + ". Release the GPU when it isn't used (scale to zero, shut notebooks down after inactivity), or share one GPU (time-slicing, MIG)." +
                 (waiting.length ? ` ${waiting.length} pod${waiting.length === 1 ? " is" : "s are"} waiting for GPUs meanwhile.` : ""),
               monthly_saving: perMonth(usd), target: `${w.namespace}/${w.workload}` });
  }
  const free = p.gpus.filter((g) => g.hours > 0 && g.free_hours / g.hours >= 0.9);
  if (free.length) {
    const usd = free.reduce((s, g) => s + g.free_hours * priceFor(g.model, prices).usd, 0);
    const nodes = [...new Set(free.map((g) => g.node))];
    out.push({ level: waiting.length ? "warning" : "info", title: `${free.length} GPU${free.length === 1 ? " was" : "s were"} unallocated almost all the time`,
               detail: (waiting.length ? `Pods are waiting for GPUs while these sit free on ${nodes.join(", ")}: they likely ask for more GPUs per node than are free there, or for another node pool. ` :
                 `On ${nodes.join(", ")}. `) + "If you don't need the spare capacity, scale the GPU node pool down or move to smaller instances.",
               monthly_saving: perMonth(usd) });
  }
  for (const s of waiting) {
    out.push({ level: "warning", title: `${s.pod} (${s.namespace}) is waiting for GPUs`,
               detail: `Kubernetes couldn't schedule it ${s.events} time${s.events === 1 ? "" : "s"} since ${new Date(s.first).toUTCString().slice(5, 22)} UTC: ${s.message}`,
               target: `${s.namespace}/${s.pod}` });
  }
  const order = { critical: 0, warning: 1, info: 2 };
  return out.sort((a, b) => order[a.level] - order[b.level] || (b.monthly_saving ?? 0) - (a.monthly_saving ?? 0));
}

export type Forecast = { days: number; fleet: number; peak_held: number; peak_active: number; never_needed: number;
  slope_per_day: number; in_30_days: number | null };

/** Demand from daily peaks: the most GPUs busy / held at once, GPUs never needed, and a linear trend
 *  of the daily busy peak projected 30 days out (needs at least 3 days). */
export function forecast(days: GpuDayRow[]): Forecast | null {
  if (!days.length) return null;
  const fleet = Math.max(...days.map((d) => d.gpus_max));
  const peakHeld = Math.max(...days.map((d) => d.held_max)), peakActive = Math.max(...days.map((d) => d.active_max));
  let slope = 0, in30: number | null = null;
  if (days.length >= 3) {
    const xs = days.map((_, i) => i), ys = days.map((d) => d.active_max);
    const mx = xs.reduce((a, b) => a + b, 0) / xs.length, my = ys.reduce((a, b) => a + b, 0) / ys.length;
    const sxx = xs.reduce((s, x) => s + (x - mx) ** 2, 0);
    slope = sxx ? xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0) / sxx : 0;
    in30 = Math.max(0, Math.round(my + slope * (xs.length - 1 - mx + 30)));
  }
  return { days: days.length, fleet, peak_held: peakHeld, peak_active: peakActive, never_needed: Math.max(0, fleet - peakHeld),
           slope_per_day: slope, in_30_days: in30 };
}

function weighted(pairs: [number, number][]): number {
  const w = pairs.reduce((s, [, h]) => s + h, 0);
  return w ? pairs.reduce((s, [v, h]) => s + v * h, 0) / w : 0;
}
