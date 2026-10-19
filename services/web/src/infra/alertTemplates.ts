// Ready-made alert rules for Kubernetes, GPUs and AI agents: "A query crosses a threshold" rules, filled
// in from the alert form's template list. Each fires per series (a pod, a node, a GPU, a tool, a model).
// Checked on the real engine by services/compaction/test_infra_queries.py (Kubernetes, GPUs) and
// test_agents.py (AI agents).
import { HOT_TEMP } from "./model";

export type AlertTemplate = { group: "Kubernetes" | "GPUs" | "AI agents"; name: string; promql: string; op: ">" | "<"; critical: number;
  degraded?: number; for_minutes: number; help: string };

const POD = `"k8s.namespace.name", "k8s.pod.name"`;
const AGENT_SPANS = (op: string, extra = "") => `leasyd.spans{"gen_ai.operation.name"="${op}"${extra}}`;

export const ALERT_TEMPLATES: AlertTemplate[] = [
  { group: "Kubernetes", name: "Pod crash-looping", op: ">", critical: 3, degraded: 1, for_minutes: 0,
    promql: `sum by (${POD}) (max_over_time({"k8s.container.restarts"}[15m]) - min_over_time({"k8s.container.restarts"}[15m]))`,
    help: "Container restarts in the last 15 minutes, per pod" },
  { group: "Kubernetes", name: "Pod stuck pending", op: ">", critical: 0, for_minutes: 10,
    promql: `count by (${POD}) (last_over_time({"k8s.pod.phase"}[5m]) == 1)`,
    help: "A pod that can't be scheduled or started for 10 minutes" },
  { group: "Kubernetes", name: "Pod near its memory limit", op: ">", critical: 0.95, degraded: 0.85, for_minutes: 5,
    promql: `max by (${POD}) (last_over_time({"k8s.pod.memory_limit_utilization"}[5m]))`,
    help: "Memory as a share of the pod's limit: next comes an OOM kill" },
  { group: "Kubernetes", name: "Node not ready", op: "<", critical: 1, for_minutes: 3,
    promql: `min by ("k8s.node.name") (last_over_time({"k8s.node.condition_ready"}[5m]))`,
    help: "The node's Ready condition is false or unknown" },
  { group: "Kubernetes", name: "Node memory high", op: ">", critical: 0.95, degraded: 0.85, for_minutes: 10,
    promql: `sum by ("k8s.node.name") (last_over_time({"k8s.node.memory.working_set"}[5m])) / sum by ("k8s.node.name") (last_over_time({"k8s.node.allocatable_memory"}[5m]))`,
    help: "Memory in use over what the node offers pods" },
  { group: "Kubernetes", name: "Deployment missing replicas", op: ">", critical: 0, for_minutes: 10,
    promql: `sum by ("k8s.namespace.name", "k8s.deployment.name") (last_over_time({"k8s.deployment.desired"}[5m])) - sum by ("k8s.namespace.name", "k8s.deployment.name") (last_over_time({"k8s.deployment.available"}[5m]))`,
    help: "Replicas wanted but not available" },
  { group: "GPUs", name: "GPU XID error", op: ">", critical: 0, for_minutes: 0,
    promql: `max by (hostname, gpu, UUID) (max_over_time({"DCGM_FI_DEV_XID_ERRORS"}[5m]))`,
    help: "Any XID the driver reports (the value is the code; what to do: GPUs > Health)" },
  { group: "GPUs", name: "GPU double-bit ECC errors", op: ">", critical: 0, for_minutes: 0,
    promql: `max by (hostname, gpu, UUID) (max_over_time({"DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"}[10m]) - min_over_time({"DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"}[10m]))`,
    help: "Uncorrectable memory errors in the last 10 minutes" },
  { group: "GPUs", name: "GPU too hot", op: ">", critical: 90, degraded: HOT_TEMP, for_minutes: 5,
    promql: `max by (hostname, gpu) (max_over_time({"DCGM_FI_DEV_GPU_TEMP"}[5m]))`,
    help: "GPU temperature, °C" },
  { group: "GPUs", name: "GPU thermal throttling", op: ">", critical: 0.25, degraded: 0.05, for_minutes: 10,
    promql: `max by (hostname, gpu) (rate({"DCGM_FI_DEV_THERMAL_VIOLATION"}[5m])) / 1e9`,
    help: "Share of time the GPU slowed itself down for heat" },
  { group: "GPUs", name: "GPU held but idle", op: "<", critical: 1, degraded: 5, for_minutes: 60,
    promql: `avg by (namespace, pod, UUID) (avg_over_time({"DCGM_FI_DEV_GPU_UTIL", pod!=""}[1h]))`,
    help: "A pod holds a GPU and keeps it under 5% busy for an hour (paid, unused)" },
  { group: "AI agents", name: "Agent tool errors", op: ">", critical: 0, for_minutes: 0,
    promql: `sum by ("gen_ai.tool.name") (increase(${AGENT_SPANS("execute_tool", ', status_code="ERROR"')}[10m]))`,
    help: "Failed tool calls in the last 10 minutes, per tool: often the agent answers as if it worked (see AI agents > Issues)" },
  { group: "AI agents", name: "LLM call errors", op: ">", critical: 3, degraded: 0, for_minutes: 0,
    promql: `sum by ("gen_ai.request.model") (increase(${AGENT_SPANS("chat", ', status_code="ERROR"')}[10m]))`,
    help: "Failed model calls (rate limits, timeouts) in the last 10 minutes, per model" },
  { group: "AI agents", name: "Slow LLM calls", op: ">", critical: 30, degraded: 15, for_minutes: 5,
    promql: `histogram_quantile(0.95, sum by ("gen_ai.request.model") (rate(leasyd.span.duration{"gen_ai.operation.name"="chat"}[10m])))`,
    help: "95th percentile of model call durations, seconds, per model" },
];
