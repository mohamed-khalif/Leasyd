// Kubernetes and GPU monitoring: the names, thresholds and tables every part of the app reads from
// (pages, built-in dashboards, alert templates). The data contract is docs/gpu-kubernetes.md; the
// collectors that send it are in examples/kubernetes. No React here, no imports: queries.ts and
// the contract test (services/compaction/test_infra_queries.py) use it as is.

/** Below this GPU utilization (%) a GPU is not doing work: idle if a workload holds it, else free. */
export const IDLE_UTIL = 5;
/** GPU temperature (C) worth a look; most data-center GPUs slow themselves down from ~85-90 C. */
export const HOT_TEMP = 85;

export const DCGM = {
  util: "DCGM_FI_DEV_GPU_UTIL",                       // %, every GPU
  engine: "DCGM_FI_PROF_GR_ENGINE_ACTIVE",            // 0-1, GPUs with profiling (data-center)
  sm: "DCGM_FI_PROF_SM_ACTIVE",
  tensor: "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE",
  memUsed: "DCGM_FI_DEV_FB_USED",                     // MiB
  memFree: "DCGM_FI_DEV_FB_FREE",                     // MiB
  temp: "DCGM_FI_DEV_GPU_TEMP",                       // C
  memTemp: "DCGM_FI_DEV_MEMORY_TEMP",
  power: "DCGM_FI_DEV_POWER_USAGE",                   // W
  xid: "DCGM_FI_DEV_XID_ERRORS",                      // the last XID seen (0: none)
  dbe: "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",               // counters
  sbe: "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL",
  remapFailure: "DCGM_FI_DEV_ROW_REMAP_FAILURE",      // 1: rows can't be remapped any more
  uncorrectableRows: "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
  pcieReplay: "DCGM_FI_DEV_PCIE_REPLAY_COUNTER",
  thermal: "DCGM_FI_DEV_THERMAL_VIOLATION",           // ns throttled, counter
  powerCap: "DCGM_FI_DEV_POWER_VIOLATION",
} as const;

export const K8S = {
  nodeCpu: "k8s.node.cpu.usage",                      // cores (kubeletstats)
  nodeMem: "k8s.node.memory.working_set",             // bytes
  nodeMemAvailable: "k8s.node.memory.available",
  nodeFsUsed: "k8s.node.filesystem.usage",
  nodeFsCap: "k8s.node.filesystem.capacity",
  podCpu: "k8s.pod.cpu.usage",
  podMem: "k8s.pod.memory.working_set",
  podCpuLimit: "k8s.pod.cpu_limit_utilization",       // 0-1 of the limit
  podMemLimit: "k8s.pod.memory_limit_utilization",
  podCpuRequest: "k8s.pod.cpu_request_utilization",
  podMemRequest: "k8s.pod.memory_request_utilization",
  allocCpu: "k8s.node.allocatable_cpu",               // k8s_cluster
  allocMem: "k8s.node.allocatable_memory",
  allocPods: "k8s.node.allocatable_pods",
  ready: "k8s.node.condition_ready",                  // 1 ready, 0 not, -1 unknown
  condition: "k8s.node.condition",                    // newer receivers: one metric, attribute "condition"
  phase: "k8s.pod.phase",                             // 1 Pending 2 Running 3 Succeeded 4 Failed 5 Unknown
  restarts: "k8s.container.restarts",
  containerReady: "k8s.container.ready",
  reason: "k8s.container.status.reason",              // attribute k8s.container.status.reason
  cpuRequest: "k8s.container.cpu_request",
  cpuLimit: "k8s.container.cpu_limit",
  memRequest: "k8s.container.memory_request",
  memLimit: "k8s.container.memory_limit",
} as const;

export const WORKLOAD_METRICS: [kind: string, desired: string, ready: string][] = [
  ["Deployment", "k8s.deployment.desired", "k8s.deployment.available"],
  ["StatefulSet", "k8s.statefulset.desired_pods", "k8s.statefulset.ready_pods"],
  ["DaemonSet", "k8s.daemonset.desired_scheduled_nodes", "k8s.daemonset.ready_nodes"],
  ["Job", "k8s.job.active_pods", "k8s.job.successful_pods"],
];

export const PHASES: Record<number, string> = { 1: "Pending", 2: "Running", 3: "Succeeded", 4: "Failed", 5: "Unknown" };
/** Container waiting / terminated reasons that mean trouble. */
export const BAD_REASONS = ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "OOMKilled",
  "Error", "ContainerCannotRun"];

/** What an XID means and what to do: NVIDIA's XID catalog, the ones that matter in practice. */
export const XID: Record<number, { title: string; level: "critical" | "warning" | "info"; action: string }> = {
  13: { title: "Graphics engine exception", level: "warning", action: "Usually the application (bad memory access or an illegal instruction). Check the workload's logs; if it repeats with different workloads on this GPU, run DCGM diagnostics (dcgmi diag -r 3)." },
  31: { title: "GPU memory page fault", level: "warning", action: "Usually an application bug (illegal memory access). Check and restart the workload." },
  32: { title: "Invalid or corrupted push buffer stream", level: "warning", action: "Driver or PCIe problem. Update the driver; if it repeats, check the PCIe link (replay counter) and reseat the GPU." },
  38: { title: "Driver firmware error", level: "warning", action: "Update the driver; reboot the node if it repeats." },
  43: { title: "GPU stopped processing", level: "warning", action: "An application error stopped the GPU's work. Restart the workload." },
  45: { title: "Preemptive cleanup", level: "info", action: "Follows an earlier error or a killed process: look at the XID before it." },
  48: { title: "Double-bit ECC error", level: "critical", action: "Uncorrectable memory error: drain the node and reset the GPU (nvidia-smi -r) or reboot. If it happens again, have the GPU replaced." },
  61: { title: "Internal micro-controller breakpoint", level: "warning", action: "Reset the GPU and update the driver; if it repeats, replace the GPU." },
  62: { title: "Internal micro-controller halt", level: "critical", action: "Reset the GPU and update the driver; if it repeats, replace the GPU." },
  63: { title: "Row remapping (ECC) recorded", level: "warning", action: "Memory rows were remapped; reset the GPU when you can so the remap takes effect." },
  64: { title: "Row remapping failed", level: "critical", action: "The GPU can't remap bad memory any more: take it out of service and replace it." },
  68: { title: "Video processor exception", level: "warning", action: "Encoder / decoder fault, usually the application. Restart the workload." },
  69: { title: "Graphics engine class error", level: "warning", action: "Usually the application. Check the workload; update the driver if it repeats." },
  74: { title: "NVLink error", level: "critical", action: "Check NVLink health (nvidia-smi nvlink -e); reset the GPUs; if it repeats, the link or board needs service." },
  79: { title: "GPU has fallen off the bus", level: "critical", action: "Drain the node and reboot. Check power and PCIe seating; if it repeats, replace the GPU." },
  92: { title: "High single-bit ECC error rate", level: "warning", action: "Memory is degrading: plan to replace the GPU." },
  94: { title: "Contained ECC error", level: "warning", action: "Only the affected application was stopped; restart it. The GPU stays usable." },
  95: { title: "Uncontained ECC error", level: "critical", action: "Every application on the GPU is affected: drain the node and reset the GPU." },
  109: { title: "Context switch timeout", level: "warning", action: "A workload hung the GPU. Restart the workload; update the driver if it repeats." },
  119: { title: "GSP RPC timeout", level: "critical", action: "Reset the GPU; update the driver. If it repeats, NVIDIA suggests turning off GSP firmware." },
  120: { title: "GSP error", level: "critical", action: "Reset the GPU; update the driver. If it repeats, NVIDIA suggests turning off GSP firmware." },
  140: { title: "Unrecovered ECC error", level: "critical", action: "Drain the node and reset the GPU; replace it if it repeats." },
};
export const xidInfo = (id: number) => XID[id] ?? { title: `XID ${id}`, level: "warning" as const, action: "See NVIDIA's XID catalog for this code; check the workload's logs and the node's kernel log (dmesg)." };

/** Default price per GPU hour (USD): AWS on-demand list prices, us-east-1, of the instances with
 *  that GPU (per GPU). Estimates; an account's owners set their own in Settings > GPU prices. */
export const DEFAULT_GPU_PRICES: [pattern: string, label: string, usdPerHour: number][] = [
  ["B200", "NVIDIA B200", 14.24],
  ["H200", "NVIDIA H200", 7.91],
  ["H100", "NVIDIA H100", 6.88],
  ["A100.*80", "NVIDIA A100 80GB", 3.43],
  ["A100", "NVIDIA A100 40GB", 2.74],
  ["L40S", "NVIDIA L40S", 1.86],
  ["A10G", "NVIDIA A10G", 1.01],
  ["L4\\b", "NVIDIA L4", 0.80],
  ["V100", "NVIDIA V100", 3.06],
  ["T4\\b", "NVIDIA T4", 0.53],
];
export const FALLBACK_GPU_PRICE = 2.0;

export type GpuPrice = { pattern: string; label: string; usd_per_hour: number };

/** Price per hour of a GPU model: the account's own price first, then the defaults. */
export function priceFor(model: string, own: GpuPrice[] = []): { usd: number; label: string; own: boolean } {
  for (const p of own) {
    if (safeTest(p.pattern, model)) return { usd: p.usd_per_hour, label: p.label, own: true };
  }
  for (const [pattern, label, usd] of DEFAULT_GPU_PRICES) {
    if (safeTest(pattern, model)) return { usd, label, own: false };
  }
  return { usd: FALLBACK_GPU_PRICE, label: model || "unknown GPU", own: false };
}

function safeTest(pattern: string, s: string): boolean {
  try { return new RegExp(pattern, "i").test(s); } catch { return s.toLowerCase().includes(pattern.toLowerCase()); }
}

/** Kubernetes pod names: the workload from a pod's name when nothing better is known
 *  (checkout-7d9f8b6c5-x2x7k -> checkout, node-exporter-x2x7k -> node-exporter). */
export function workloadFromPod(pod: string): string {
  return pod.replace(/-[a-z0-9]{6,10}-[a-z0-9]{5}$/, "").replace(/-[a-z0-9]{5}$/, "");
}
