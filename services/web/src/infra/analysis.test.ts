// node:test over analysis.ts and model.ts (run: npm test).
import assert from "node:assert/strict";
import { test } from "node:test";
import { byModel, byTeam, fleetSpend, forecast, now, recommendations } from "./analysis";
import { priceFor, workloadFromPod, xidInfo } from "./model";
import type { GpuRow, GpuWorkloadRow } from "./queries";

const gpu = (o: Partial<GpuRow>): GpuRow => ({ gpu: "GPU-1", model: "NVIDIA A10G", node: "n1", cluster: "c", instance_type: "", cloud: "",
  hours: 10, active_hours: 10, idle_hours: 0, free_hours: 0, util_avg: 50, util_now: 50, mem_used_now: 1, mem_total: 2, temp_max: 60,
  power_avg: 100, holder_now: "ns/p", team_now: "", last_seen: "", ...o });
const wl = (o: Partial<GpuWorkloadRow>): GpuWorkloadRow => ({ namespace: "ns", workload: "w", kind: "Deployment", team: "", model: "NVIDIA A10G",
  gpus: 1, pods: 1, hours: 10, active_hours: 10, idle_hours: 0, util_avg: 50, holding_now: 1, ...o });

test("prices: own first, then the defaults by model name, else a fallback", () => {
  assert.equal(priceFor("NVIDIA A100-SXM4-80GB").usd, 3.43);
  assert.equal(priceFor("NVIDIA A100-SXM4-40GB").usd, 2.74);
  assert.equal(priceFor("Tesla T4").usd, 0.53);
  assert.equal(priceFor("NVIDIA L4").usd, 0.8);
  assert.equal(priceFor("NVIDIA L40S").usd, 1.86);                       // not L4
  assert.equal(priceFor("NVIDIA H100 80GB HBM3").usd, 6.88);
  assert.deepEqual(priceFor("NVIDIA A10G", [{ pattern: "A10G", label: "ours", usd_per_hour: 0.7 }]), { usd: 0.7, label: "ours", own: true });
  assert.equal(priceFor("Mystery GPU").usd, 2.0);
  assert.equal(priceFor("x", [{ pattern: "(", label: "bad regex", usd_per_hour: 1 }]).usd, 2.0);   // a bad pattern never throws
});

test("spend splits hours by use; models and now", () => {
  const gs = [gpu({}), gpu({ gpu: "GPU-2", active_hours: 0, idle_hours: 10, util_now: 1, util_avg: 1 }),
              gpu({ gpu: "GPU-3", node: "n2", active_hours: 0, free_hours: 10, util_now: 0, util_avg: 0, holder_now: "" })];
  const s = fleetSpend(gs);
  assert.ok(Math.abs(s.total - 30.3) < 1e-9 && Math.abs(s.idle - 10.1) < 1e-9 && Math.abs(s.free - 10.1) < 1e-9);
  assert.deepEqual(now(gs), { active: 1, idle: 1, free: 1 });
  const m = byModel(gs);
  assert.equal(m.length, 1); assert.equal(m[0].gpus, 3); assert.equal(m[0].nodes, 2); assert.ok(Math.abs(m[0].util - 17) < 1e-9);
});

test("teams: label, else namespace", () => {
  const t = byTeam([wl({ team: "search" }), wl({ team: "search", workload: "x", idle_hours: 5, active_hours: 5 }), wl({ namespace: "ml" })]);
  assert.deepEqual(t.map((r) => [r.team, r.workloads, Math.round(r.spend * 100) / 100]), [["search", 2, 20.2], ["ml (namespace)", 1, 10.1]]);
  assert.ok(Math.abs(t[0].idle_spend - 5.05) < 1e-9);
});

test("recommendations: hardware first, then idle holders by saving, spare GPUs, waiting pods", () => {
  const recs = recommendations({
    rangeHours: 10,
    gpus: [gpu({ gpu: "F", holder_now: "", free_hours: 10, active_hours: 0 })],
    workloads: [wl({ workload: "nb", idle_hours: 9, active_hours: 1 }), wl({ workload: "busy" })],
    stalled: [{ namespace: "ml", pod: "batch", first: "2026-10-14T10:00:00Z", last: "", events: 3, message: "Insufficient nvidia.com/gpu", phase: 1 }],
    health: [{ gpu: "X", model: "A100", node: "n9", holder: "", xids: [48, 45], xid_last: null, dbe: 1, sbe: 0, remap_failure: 0,
               uncorrectable_rows: 0, pcie_replays: 0, thermal_s: 0, power_s: 0, temp_max: 60, mem_temp_max: 60 }],
  });
  assert.deepEqual(recs.map((r) => r.level), ["critical", "warning", "warning", "warning"]);
  assert.match(recs[0].title, /XID 48/);                       // XID 45 (info) left out; the DBE is the XID's story
  assert.match(recs[1].title, /unallocated/);                 // warnings: the bigger saving first
  assert.match(recs[2].title, /nb .* holds 1 GPU/);
  assert.ok(Math.abs((recs[2].monthly_saving ?? 0) - 9 * 1.01 / 10 * 730) < 1e-6);
  assert.match(recs[3].title, /batch .* waiting/);
  assert.deepEqual(recommendations({ rangeHours: 0, gpus: [], workloads: [], stalled: [], health: [] }), []);
});

test("forecast: peaks, never needed, trend", () => {
  const d = (active: number, i: number) => ({ day: `2026-10-0${i + 1}`, gpus_max: 16, held_max: 14, active_max: active, active_p95: active, active_avg: active });
  const f = forecast([6, 8, 10, 12].map(d))!;
  assert.equal(f.fleet, 16); assert.equal(f.never_needed, 2); assert.equal(f.peak_active, 12);
  assert.equal(f.slope_per_day, 2); assert.equal(f.in_30_days, 72);
  assert.equal(forecast([d(5, 0)])!.in_30_days, null);
  assert.equal(forecast([]), null);
});

test("names", () => {
  assert.equal(workloadFromPod("checkout-7d9f8b6c5-x2x7k"), "checkout");
  assert.equal(workloadFromPod("node-exporter-x2x7k"), "node-exporter");
  assert.equal(workloadFromPod("jupyter-alice"), "jupyter-alice");
  assert.equal(workloadFromPod("llm-finetune-worker-0"), "llm-finetune-worker-0");
  assert.equal(xidInfo(79).level, "critical");
  assert.equal(xidInfo(999).title, "XID 999");
});
