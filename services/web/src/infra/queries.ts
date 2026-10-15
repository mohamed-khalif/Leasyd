// Every SQL query the Kubernetes and GPU pages send, as plain functions (no React), so the contract
// test (services/compaction/test_infra_queries.py) runs each one on the real query engine over
// real collector data and the demo cluster. Each query aggregates in SQL: results stay far below
// the engine's 10,000-row limit however many points the range holds.
//
// `uses` says which data a query reads; the page passes the services carrying that data as the
// engine's "services" hint (see discovery()), so an account's app data isn't read for these pages.
import { DCGM, IDLE_UTIL, K8S, K8S_SUFFIX, WORKLOAD_METRICS } from "./model";

export type Uses = "gpu" | "k8s" | "events";
export type Q = { sql: string; uses: Uses[] };
export type Filter = { cluster?: string | null; namespace?: string | null };

export const q = (s: string) => `'${s.replace(/'/g, "''")}'`;
const A = (k: string) => `attributes['${k}']`;
const R = (k: string) => `resource_attributes['${k}']`;
const list = (xs: readonly string[]) => xs.map(q).join(", ");
const S = (k: string) => `coalesce(${A(k)}, '')`;   // a label, '' when absent (arg_max keeps '' but skips NULL)

// ------------------------------------------------------------------ shared expressions

/** One GPU: its UUID, else host/index. */
const GPU = `coalesce(${A("UUID")}, ${A("Hostname")} || '/' || ${A("gpu")})`;
/** Its node: the agent's "node" label, else dcgm-exporter's Hostname. */
const GPU_NODE = `coalesce(${A("node")}, ${A("Hostname")}, ${R("k8s.node.name")}, ${R("host.name")}, '')`;
/** The workload holding it: a pod (namespace/pod), else a Slurm job, else none (''). */
const HOLDER = `CASE WHEN ${S("pod")} <> '' THEN ${S("namespace")} || '/' || ${S("pod")}
  WHEN ${S("hpc_job")} <> '' THEN 'slurm/' || ${S("hpc_job")} ELSE '' END`;
const TEAM = `coalesce(nullif(${S("team")}, ''), nullif(${S("label_team")}, ''), '')`;
const OWNER = `coalesce(${R("k8s.deployment.name")}, ${R("k8s.statefulset.name")}, ${R("k8s.daemonset.name")},
  ${R("k8s.cronjob.name")}, ${R("k8s.job.name")})`;
const OWNER_KIND = `CASE WHEN ${R("k8s.deployment.name")} IS NOT NULL THEN 'Deployment'
  WHEN ${R("k8s.statefulset.name")} IS NOT NULL THEN 'StatefulSet' WHEN ${R("k8s.daemonset.name")} IS NOT NULL THEN 'DaemonSet'
  WHEN ${R("k8s.cronjob.name")} IS NOT NULL THEN 'CronJob' WHEN ${R("k8s.job.name")} IS NOT NULL THEN 'Job' END`;

function where(f: Filter, nsKey: string | null = R("k8s.namespace.name"), clusterKey = R("k8s.cluster.name")): string {
  const out: string[] = [];
  if (f.cluster) out.push(`${clusterKey} = ${q(f.cluster)}`);
  if (f.namespace && nsKey) out.push(`${nsKey} = ${q(f.namespace)}`);
  return out.length ? ` AND ${out.join(" AND ")}` : "";
}

/** Bucket size for GPU time: ~5 min for a day or less, 1 h above (GPU hours = buckets with data). */
export function gpuBucketSeconds(rangeMinutes: number): number {
  return rangeMinutes <= 360 ? 60 : rangeMinutes <= 1440 ? 300 : rangeMinutes <= 10080 ? 900 : 3600;
}

/** Per GPU and bucket: utilization (avg), memory, temperature, power, and who held it at the bucket's end. */
function gpuBuckets(b: number, f: Filter): string {
  return `SELECT ${GPU} AS gpu, time_bucket(INTERVAL '${b} seconds', ts) AS t,
      any_value(${S("modelName")}) AS model, any_value(${GPU_NODE}) AS node, any_value(coalesce(${R("k8s.cluster.name")}, '')) AS cluster,
      any_value(coalesce(${R("host.type")}, '')) AS instance_type, any_value(coalesce(${R("cloud.provider")}, '')) AS cloud,
      avg(value) FILTER (WHERE metric_name = '${DCGM.util}') AS util,
      avg(value) FILTER (WHERE metric_name = '${DCGM.memUsed}') AS mem_used,
      avg(value) FILTER (WHERE metric_name = '${DCGM.memFree}') AS mem_free,
      max(value) FILTER (WHERE metric_name = '${DCGM.temp}') AS temp,
      avg(value) FILTER (WHERE metric_name = '${DCGM.power}') AS power,
      arg_max(${HOLDER}, ts) FILTER (WHERE metric_name = '${DCGM.util}') AS holder,
      arg_max(${TEAM}, ts) FILTER (WHERE metric_name = '${DCGM.util}') AS team
    FROM metrics
    WHERE metric_name IN (${list([DCGM.util, DCGM.memUsed, DCGM.memFree, DCGM.temp, DCGM.power])})${where(f, A("namespace"))}
    GROUP BY 1, 2`;
}

// ------------------------------------------------------------------ discovery

/** Which services carry GPU, Kubernetes and event data (a short, recent window: cheap). */
export function discovery(): Q {
  return {
    uses: [],
    sql: `SELECT 'gpu' AS kind, service, count(*) AS n FROM metrics WHERE metric_name = '${DCGM.util}' GROUP BY 2
UNION ALL SELECT 'k8s', service, count(*) FROM metrics WHERE metric_name IN (${list([K8S.nodeCpu, K8S.podCpu, K8S.phase, K8S.allocCpu])}) GROUP BY 2
UNION ALL SELECT 'events', service, count(*) FROM logs WHERE ${A("k8s.event.reason")} IS NOT NULL GROUP BY 2`,
  };
}

/** Clusters and namespaces seen (for the filters). */
export function clusters(): Q {
  return {
    uses: ["k8s", "gpu"],
    sql: `SELECT coalesce(${R("k8s.cluster.name")}, '') AS cluster, coalesce(${R("k8s.namespace.name")}, ${A("namespace")}, '') AS namespace,
      count(*) AS n
FROM metrics WHERE metric_name IN (${list([K8S.podCpu, K8S.phase, DCGM.util])})
GROUP BY 1, 2 ORDER BY 1, 2`,
  };
}

// ------------------------------------------------------------------ GPUs

export type GpuRow = { gpu: string; model: string; node: string; cluster: string; instance_type: string; cloud: string;
  hours: number; active_hours: number; idle_hours: number; free_hours: number; util_avg: number; util_now: number | null;
  mem_used_now: number | null; mem_total: number | null; temp_max: number | null; power_avg: number | null;
  holder_now: string; team_now: string; last_seen: string };

/** One row per GPU: what it is, where, who holds it now, and its time split into active / idle / free. */
export function gpuFleet(rangeMinutes: number, f: Filter = {}): Q {
  const b = gpuBucketSeconds(rangeMinutes), h = `${b} / 3600.0`;
  return {
    uses: ["gpu"],
    sql: `WITH b AS (${gpuBuckets(b, f)})
SELECT gpu, any_value(model) AS model, any_value(node) AS node, any_value(cluster) AS cluster,
  any_value(instance_type) AS instance_type, any_value(cloud) AS cloud,
  count(util) * ${h} AS hours,
  count(*) FILTER (WHERE util >= ${IDLE_UTIL}) * ${h} AS active_hours,
  count(*) FILTER (WHERE util < ${IDLE_UTIL} AND holder <> '') * ${h} AS idle_hours,
  count(*) FILTER (WHERE util < ${IDLE_UTIL} AND holder = '') * ${h} AS free_hours,
  avg(util) AS util_avg, arg_max(util, t) AS util_now,
  arg_max(mem_used, t) AS mem_used_now, max(mem_used + mem_free) AS mem_total,
  max(temp) AS temp_max, avg(power) AS power_avg,
  arg_max(holder, t) AS holder_now, arg_max(team, t) AS team_now, max(t) AS last_seen
FROM b WHERE util IS NOT NULL
GROUP BY gpu ORDER BY node, gpu LIMIT 5000`,
  };
}

export type GpuSeriesRow = { t: string; gpus: number; active: number; idle: number; free: number; util: number; mem: number | null };

/** The fleet over time: GPUs active / idle (held, unused) / free, average utilization and memory use. */
export function gpuSeries(rangeMinutes: number, f: Filter = {}): Q {
  const b = gpuBucketSeconds(rangeMinutes);
  return {
    uses: ["gpu"],
    sql: `WITH b AS (${gpuBuckets(b, f)})
SELECT t, count(*) AS gpus,
  count(*) FILTER (WHERE util >= ${IDLE_UTIL}) AS active,
  count(*) FILTER (WHERE util < ${IDLE_UTIL} AND holder <> '') AS idle,
  count(*) FILTER (WHERE util < ${IDLE_UTIL} AND holder = '') AS free,
  avg(util) AS util, sum(mem_used) / nullif(sum(mem_used + mem_free), 0) AS mem
FROM b WHERE util IS NOT NULL GROUP BY t ORDER BY t`,
  };
}

export type GpuWorkloadRow = { namespace: string; workload: string; kind: string; team: string; model: string;
  gpus: number; pods: number; hours: number; active_hours: number; idle_hours: number; util_avg: number; holding_now: number };

/** GPU time by workload (and model, for its price): who uses the GPUs, and who holds them idle.
 *  Workload and team come from the pod's owner (kubelet stats, k8sattributes), else its name. */
export function gpuWorkloads(rangeMinutes: number, f: Filter = {}): Q {
  const b = gpuBucketSeconds(rangeMinutes), h = `${b} / 3600.0`;
  return {
    uses: ["gpu", "k8s"],
    sql: `WITH b AS (${gpuBuckets(b, f)}),
owners AS (
  SELECT ${R("k8s.namespace.name")} AS ns, ${R("k8s.pod.name")} AS pod, any_value(${OWNER}) AS workload, any_value(${OWNER_KIND}) AS kind,
    any_value(${R("k8s.pod.labels.team")}) AS team
  FROM metrics WHERE metric_name = '${K8S.podMem}' GROUP BY 1, 2),
held AS (
  SELECT b.*, split_part(holder, '/', 1) AS ns, substr(holder, length(split_part(holder, '/', 1)) + 2) AS pod FROM b
  WHERE util IS NOT NULL AND holder <> '')
SELECT held.ns AS namespace,
  coalesce(o.workload, CASE WHEN held.ns = 'slurm' THEN held.pod
    ELSE regexp_replace(regexp_replace(held.pod, '-${K8S_SUFFIX}{6,10}-${K8S_SUFFIX}{5}$', ''), '-${K8S_SUFFIX}{5}$', '') END) AS workload,
  coalesce(o.kind, CASE WHEN held.ns = 'slurm' THEN 'Slurm job' ELSE '' END) AS kind,
  coalesce(nullif(any_value(held.team), ''), any_value(o.team), '') AS team, held.model AS model,
  count(DISTINCT held.gpu) AS gpus, count(DISTINCT held.pod) AS pods,
  count(*) * ${h} AS hours,
  count(*) FILTER (WHERE util >= ${IDLE_UTIL}) * ${h} AS active_hours,
  count(*) FILTER (WHERE util < ${IDLE_UTIL}) * ${h} AS idle_hours,
  avg(util) AS util_avg,
  count(DISTINCT held.gpu) FILTER (WHERE t = (SELECT max(t) FROM b)) AS holding_now
FROM held LEFT JOIN owners o ON o.ns = held.ns AND o.pod = held.pod
GROUP BY 1, 2, 3, 5 ORDER BY hours DESC, namespace, workload, model LIMIT 2000`,
  };
}

export type GpuHealthRow = { gpu: string; model: string; node: string; holder: string; xids: number[] | null; xid_last: string | null;
  dbe: number | null; sbe: number | null; remap_failure: number | null; uncorrectable_rows: number | null; pcie_replays: number | null;
  thermal_s: number | null; power_s: number | null; temp_max: number | null; mem_temp_max: number | null };

/** GPUs with a hardware sign in the range: XIDs, ECC errors, failed row remapping, PCIe replays,
 *  throttling (thermal / power), or a high temperature. Counters: rise over the range. */
export function gpuHealth(f: Filter = {}): Q {
  const rise = (m: string) => `(max(value) FILTER (WHERE metric_name = '${m}') - min(value) FILTER (WHERE metric_name = '${m}'))`;
  return {
    uses: ["gpu"],
    sql: `SELECT ${GPU} AS gpu, any_value(${S("modelName")}) AS model, any_value(${GPU_NODE}) AS node,
  arg_max(${HOLDER}, ts) AS holder,
  list(DISTINCT CAST(value AS BIGINT)) FILTER (WHERE metric_name = '${DCGM.xid}' AND value > 0) AS xids,
  max(ts) FILTER (WHERE metric_name = '${DCGM.xid}' AND value > 0) AS xid_last,
  ${rise(DCGM.dbe)} AS dbe, ${rise(DCGM.sbe)} AS sbe,
  max(value) FILTER (WHERE metric_name = '${DCGM.remapFailure}') AS remap_failure,
  ${rise(DCGM.uncorrectableRows)} AS uncorrectable_rows, ${rise(DCGM.pcieReplay)} AS pcie_replays,
  ${rise(DCGM.thermal)} / 1e9 AS thermal_s, ${rise(DCGM.powerCap)} / 1e9 AS power_s,
  max(value) FILTER (WHERE metric_name = '${DCGM.temp}') AS temp_max,
  max(value) FILTER (WHERE metric_name = '${DCGM.memTemp}') AS mem_temp_max
FROM metrics
WHERE metric_name IN (${list([DCGM.xid, DCGM.dbe, DCGM.sbe, DCGM.remapFailure, DCGM.uncorrectableRows, DCGM.pcieReplay, DCGM.thermal,
    DCGM.powerCap, DCGM.temp, DCGM.memTemp])})${where(f, A("namespace"))}
GROUP BY 1
HAVING xids IS NOT NULL OR coalesce(dbe, 0) > 0 OR coalesce(remap_failure, 0) > 0 OR coalesce(uncorrectable_rows, 0) > 0
  OR coalesce(pcie_replays, 0) > 0 OR coalesce(thermal_s, 0) > 0 OR coalesce(power_s, 0) > 0 OR coalesce(temp_max, 0) >= 85
ORDER BY xid_last DESC NULLS LAST, dbe DESC NULLS LAST, gpu LIMIT 1000`,
  };
}

export type XidRow = { gpu: string; model: string; node: string; xid: number; first: string; last: string; holder: string };

/** Each XID a GPU reported (a GPU repeats its last XID while it lasts): first and last seen. */
export function gpuXids(f: Filter = {}): Q {
  return {
    uses: ["gpu"],
    sql: `SELECT ${GPU} AS gpu, any_value(${S("modelName")}) AS model, any_value(${GPU_NODE}) AS node, CAST(value AS BIGINT) AS xid,
  min(ts) AS first, max(ts) AS last, arg_max(${HOLDER}, ts) AS holder
FROM metrics WHERE metric_name = '${DCGM.xid}' AND value > 0${where(f, A("namespace"))}
GROUP BY 1, 4 ORDER BY last DESC, gpu, xid LIMIT 500`,
  };
}

export type StalledRow = { namespace: string; pod: string; first: string; last: string; events: number; message: string; phase: number | null };

/** Pods waiting for GPUs: Kubernetes couldn't schedule them for lack of nvidia.com/gpu. */
export function gpuStalled(f: Filter = {}): Q {
  return {
    uses: ["events", "k8s"],
    sql: `WITH ev AS (
  SELECT ${A("k8s.namespace.name")} AS ns, ${R("k8s.object.name")} AS pod, min(ts) AS first, max(ts) AS last, count(*) AS events,
    arg_max(body, ts) AS message
  FROM logs WHERE ${A("k8s.event.reason")} = 'FailedScheduling' AND body LIKE '%nvidia.com/gpu%'${where(f, A("k8s.namespace.name"))}
  GROUP BY 1, 2),
ph AS (
  SELECT ${R("k8s.namespace.name")} AS ns, ${R("k8s.pod.name")} AS pod, arg_max(value, ts) AS phase
  FROM metrics WHERE metric_name = '${K8S.phase}'${where(f)} GROUP BY 1, 2)
SELECT ev.ns AS namespace, ev.pod, ev.first, ev.last, ev.events, ev.message, CAST(ph.phase AS INTEGER) AS phase
FROM ev LEFT JOIN ph ON ph.ns = ev.ns AND ph.pod = ev.pod
ORDER BY (ph.phase = 1) DESC, ev.last DESC, ev.ns, ev.pod LIMIT 500`,
  };
}

export type GpuDayRow = { day: string; gpus_max: number; held_max: number; active_max: number; active_p95: number; active_avg: number };

/** Per day: the most GPUs present, held and busy at once (and busy's 95th percentile): for planning. */
export function gpuDays(rangeMinutes: number, f: Filter = {}): Q {
  const b = Math.min(gpuBucketSeconds(rangeMinutes), 900);
  return {
    uses: ["gpu"],
    sql: `WITH b AS (${gpuBuckets(b, f)}),
n AS (SELECT t, count(*) AS gpus, count(*) FILTER (WHERE holder <> '') AS held, count(*) FILTER (WHERE util >= ${IDLE_UTIL}) AS active
      FROM b WHERE util IS NOT NULL GROUP BY t)
SELECT CAST(date_trunc('day', t) AS DATE) AS day, max(gpus) AS gpus_max, max(held) AS held_max, max(active) AS active_max,
  quantile_cont(active, 0.95) AS active_p95, avg(active) AS active_avg
FROM n GROUP BY 1 ORDER BY 1`,
  };
}

export type GpuDetailRow = { t: string; util: number | null; engine: number | null; tensor: number | null; mem_used: number | null;
  mem_free: number | null; temp: number | null; power: number | null; holder: string };

/** One GPU over time. */
export function gpuDetail(gpu: string, bucketSeconds: number): Q {
  const m = (name: string, fn = "avg") => `${fn}(value) FILTER (WHERE metric_name = '${name}')`;
  return {
    uses: ["gpu"],
    sql: `SELECT time_bucket(INTERVAL '${bucketSeconds} seconds', ts) AS t, ${m(DCGM.util)} AS util, ${m(DCGM.engine)} AS engine,
  ${m(DCGM.tensor)} AS tensor, ${m(DCGM.memUsed)} AS mem_used, ${m(DCGM.memFree)} AS mem_free, ${m(DCGM.temp, "max")} AS temp,
  ${m(DCGM.power)} AS power, arg_max(${HOLDER}, ts) AS holder
FROM metrics WHERE metric_name IN (${list([DCGM.util, DCGM.engine, DCGM.tensor, DCGM.memUsed, DCGM.memFree, DCGM.temp, DCGM.power])})
  AND ${GPU} = ${q(gpu)}
GROUP BY 1 ORDER BY 1`,
  };
}

// ------------------------------------------------------------------ Kubernetes

export type NodeRow = { node: string; cluster: string; instance_type: string; zone: string; cpu: number | null; cpu_avg: number | null;
  mem: number | null; fs_used: number | null; fs_cap: number | null; alloc_cpu: number | null; alloc_mem: number | null;
  alloc_pods: number | null; ready: number | null; pods: number; gpus: number; last_seen: string };

/** One row per node: latest CPU and memory against what it offers, disk, readiness, pods and GPUs. */
export function k8sNodes(f: Filter = {}): Q {
  const last = (m: string) => `arg_max(value, ts) FILTER (WHERE metric_name = '${m}')`;
  return {
    uses: ["k8s", "gpu"],
    sql: `WITH n AS (
  SELECT ${R("k8s.node.name")} AS node, any_value(coalesce(${R("k8s.cluster.name")}, '')) AS cluster,
    max(coalesce(${R("host.type")}, '')) AS instance_type, max(coalesce(${R("cloud.availability_zone")}, '')) AS zone,
    ${last(K8S.nodeCpu)} AS cpu, avg(value) FILTER (WHERE metric_name = '${K8S.nodeCpu}') AS cpu_avg,
    ${last(K8S.nodeMem)} AS mem, ${last(K8S.nodeFsUsed)} AS fs_used, ${last(K8S.nodeFsCap)} AS fs_cap,
    ${last(K8S.allocCpu)} AS alloc_cpu, ${last(K8S.allocMem)} AS alloc_mem, ${last(K8S.allocPods)} AS alloc_pods,
    arg_max(value, ts) FILTER (WHERE metric_name = '${K8S.ready}' OR (metric_name = '${K8S.condition}' AND ${A("condition")} = 'Ready')) AS ready,
    max(ts) AS last_seen
  FROM metrics
  WHERE metric_name IN (${list([K8S.nodeCpu, K8S.nodeMem, K8S.nodeFsUsed, K8S.nodeFsCap, K8S.allocCpu, K8S.allocMem, K8S.allocPods, K8S.ready, K8S.condition])})
    AND ${R("k8s.node.name")} IS NOT NULL${where(f, null)}
  GROUP BY 1),
p AS (
  SELECT ${R("k8s.node.name")} AS node, count(DISTINCT ${R("k8s.pod.name")}) AS pods FROM metrics
  WHERE metric_name = '${K8S.podCpu}' AND ts >= (SELECT max(ts) FROM metrics WHERE metric_name = '${K8S.podCpu}') - INTERVAL 3 MINUTE${where(f, null)}
  GROUP BY 1),
g AS (
  SELECT ${GPU_NODE} AS node, count(DISTINCT ${GPU}) AS gpus FROM metrics WHERE metric_name = '${DCGM.util}' GROUP BY 1)
SELECT n.*, coalesce(p.pods, 0) AS pods, coalesce(g.gpus, 0) AS gpus
FROM n LEFT JOIN p USING (node) LEFT JOIN g USING (node)
ORDER BY n.cluster, n.node LIMIT 5000`,
  };
}

export type PodRow = { namespace: string; pod: string; cluster: string; node: string; workload: string; kind: string; team: string;
  phase: number | null; cpu: number | null; mem: number | null; cpu_limit: number | null; mem_limit: number | null;
  restarts: number | null; restarts_total: number | null; ready: number | null; reason: string; last_seen: string };

/** Pods present at the end of the range: usage (kubelet), phase, restarts in the range, readiness and
 *  any container waiting / terminated reason (Kubernetes API). Pending pods have no usage. */
export function k8sPods(f: Filter = {}): Q {
  const last = (m: string) => `arg_max(value, ts) FILTER (WHERE metric_name = '${m}')`;
  return {
    uses: ["k8s"],
    sql: `WITH k AS (
  SELECT ${R("k8s.namespace.name")} AS ns, ${R("k8s.pod.name")} AS pod, any_value(coalesce(${R("k8s.cluster.name")}, '')) AS cluster,
    any_value(${R("k8s.node.name")}) AS node, any_value(${OWNER}) AS workload, any_value(${OWNER_KIND}) AS kind,
    any_value(${R("k8s.pod.labels.team")}) AS team,
    ${last(K8S.podCpu)} AS cpu, ${last(K8S.podMem)} AS mem, ${last(K8S.podCpuLimit)} AS cpu_limit, ${last(K8S.podMemLimit)} AS mem_limit,
    max(ts) AS seen
  FROM metrics WHERE metric_name IN (${list([K8S.podCpu, K8S.podMem, K8S.podCpuLimit, K8S.podMemLimit])})${where(f)}
  GROUP BY 1, 2),
ph AS (
  SELECT ${R("k8s.namespace.name")} AS ns, ${R("k8s.pod.name")} AS pod, any_value(coalesce(${R("k8s.cluster.name")}, '')) AS cluster,
    arg_max(coalesce(${R("k8s.node.name")}, ''), ts) AS node, arg_max(value, ts) AS phase, max(ts) AS seen
  FROM metrics WHERE metric_name = '${K8S.phase}'${where(f)} GROUP BY 1, 2),
c AS (
  SELECT ${R("k8s.namespace.name")} AS ns, ${R("k8s.pod.name")} AS pod, ${R("k8s.container.name")} AS container,
    max(value) FILTER (WHERE metric_name = '${K8S.restarts}') - min(value) FILTER (WHERE metric_name = '${K8S.restarts}') AS restarts,
    ${last(K8S.restarts)} AS restarts_total, ${last(K8S.containerReady)} AS ready,
    max(ts) FILTER (WHERE metric_name = '${K8S.restarts}') AS seen,
    arg_max(${A("k8s.container.status.reason")}, ts) FILTER (WHERE metric_name = '${K8S.reason}' AND value > 0) AS reason,
    max(ts) FILTER (WHERE metric_name = '${K8S.reason}' AND value > 0) AS reason_seen
  FROM metrics WHERE metric_name IN (${list([K8S.restarts, K8S.containerReady, K8S.reason])})${where(f)}
  GROUP BY 1, 2, 3),
cp AS (
  SELECT ns, pod, sum(restarts) AS restarts, sum(restarts_total) AS restarts_total, min(ready) AS ready,
    coalesce(string_agg(DISTINCT reason, ', ') FILTER (WHERE reason_seen >= seen - INTERVAL 2 MINUTE), '') AS reason
  FROM c GROUP BY 1, 2),
pods AS (
  SELECT coalesce(k.ns, ph.ns) AS namespace, coalesce(k.pod, ph.pod) AS pod, coalesce(k.cluster, ph.cluster) AS cluster,
    coalesce(k.node, nullif(ph.node, ''), '') AS node, coalesce(k.workload, '') AS workload, coalesce(k.kind, '') AS kind,
    coalesce(k.team, '') AS team, CAST(ph.phase AS INTEGER) AS phase, k.cpu, k.mem, k.cpu_limit, k.mem_limit,
    greatest(k.seen, ph.seen) AS last_seen
  FROM k FULL OUTER JOIN ph ON k.ns = ph.ns AND k.pod = ph.pod)
SELECT pods.*, cp.restarts, cp.restarts_total, cp.ready, coalesce(cp.reason, '') AS reason
FROM pods LEFT JOIN cp ON cp.ns = pods.namespace AND cp.pod = pods.pod
WHERE pods.last_seen >= (SELECT max(last_seen) FROM pods) - INTERVAL 3 MINUTE
ORDER BY namespace, pod LIMIT 5000`,
  };
}

export type WorkloadRow = { namespace: string; name: string; kind: string; cluster: string; desired: number | null; ready: number | null };

/** Deployments, statefulsets, daemonsets and jobs: wanted and ready replicas (latest). */
export function k8sWorkloads(f: Filter = {}): Q {
  const kind = `CASE ${WORKLOAD_METRICS.map(([k, d, r]) => `WHEN metric_name IN (${list([d, r])}) THEN '${k}'`).join(" ")} END`;
  const desired = WORKLOAD_METRICS.map(([, d]) => d), ready = WORKLOAD_METRICS.map(([, , r]) => r);
  return {
    uses: ["k8s"],
    sql: `SELECT ${R("k8s.namespace.name")} AS namespace,
  coalesce(${R("k8s.deployment.name")}, ${R("k8s.statefulset.name")}, ${R("k8s.daemonset.name")}, ${R("k8s.job.name")}) AS name,
  ${kind} AS kind, any_value(coalesce(${R("k8s.cluster.name")}, '')) AS cluster,
  arg_max(value, ts) FILTER (WHERE metric_name IN (${list(desired)})) AS desired,
  arg_max(value, ts) FILTER (WHERE metric_name IN (${list(ready)})) AS ready
FROM metrics WHERE metric_name IN (${list([...desired, ...ready])})${where(f)}
GROUP BY 1, 2, 3 ORDER BY 1, 3, 2 LIMIT 5000`,
  };
}

export type EventRow = { namespace: string; kind: string; object: string; reason: string; events: number; first: string; last: string;
  message: string; warning: boolean };

/** Kubernetes events grouped by object and reason (warnings first). object: one object only. */
export function k8sEvents(f: Filter = {}, object?: { namespace: string; name: string }): Q {
  const obj = object ? ` AND ${A("k8s.namespace.name")} = ${q(object.namespace)} AND ${R("k8s.object.name")} = ${q(object.name)}` : "";
  return {
    uses: ["events"],
    sql: `SELECT coalesce(${A("k8s.namespace.name")}, '') AS namespace, coalesce(${R("k8s.object.kind")}, '') AS kind,
  coalesce(${R("k8s.object.name")}, '') AS object, ${A("k8s.event.reason")} AS reason, count(*) AS events,
  min(ts) AS first, max(ts) AS last, arg_max(body, ts) AS message, max(severity_number) >= 13 AS warning
FROM logs WHERE ${A("k8s.event.reason")} IS NOT NULL${where(f, A("k8s.namespace.name"))}${obj}
GROUP BY 1, 2, 3, 4 ORDER BY warning DESC, last DESC, namespace, object, reason LIMIT 500`,
  };
}

export type UsageSeriesRow = { t: string; key: string; cpu: number | null; mem: number | null };

/** CPU (cores) and memory (bytes) over time by namespace or node: per pod averaged in the bucket, then summed. */
export function k8sUsageSeries(bucketSeconds: number, by: "namespace" | "node", f: Filter = {}): Q {
  const key = by === "namespace" ? R("k8s.namespace.name") : R("k8s.node.name");
  return {
    uses: ["k8s"],
    sql: `WITH p AS (
  SELECT time_bucket(INTERVAL '${bucketSeconds} seconds', ts) AS t, ${key} AS key, ${R("k8s.pod.name")} AS pod,
    avg(value) FILTER (WHERE metric_name = '${K8S.podCpu}') AS cpu, avg(value) FILTER (WHERE metric_name = '${K8S.podMem}') AS mem
  FROM metrics WHERE metric_name IN (${list([K8S.podCpu, K8S.podMem])})${where(f)} GROUP BY 1, 2, 3)
SELECT t, key, sum(cpu) AS cpu, sum(mem) AS mem FROM p GROUP BY 1, 2 ORDER BY 1, 2`,
  };
}

export type PodSeriesRow = { t: string; cpu: number | null; mem: number | null; cpu_limit: number | null; mem_limit: number | null };

/** One pod over time. */
export function k8sPodSeries(namespace: string, pod: string, bucketSeconds: number): Q {
  const m = (name: string) => `avg(value) FILTER (WHERE metric_name = '${name}')`;
  return {
    uses: ["k8s"],
    sql: `SELECT time_bucket(INTERVAL '${bucketSeconds} seconds', ts) AS t, ${m(K8S.podCpu)} AS cpu, ${m(K8S.podMem)} AS mem,
  ${m(K8S.podCpuLimit)} AS cpu_limit, ${m(K8S.podMemLimit)} AS mem_limit
FROM metrics WHERE metric_name IN (${list([K8S.podCpu, K8S.podMem, K8S.podCpuLimit, K8S.podMemLimit])})
  AND ${R("k8s.namespace.name")} = ${q(namespace)} AND ${R("k8s.pod.name")} = ${q(pod)}
GROUP BY 1 ORDER BY 1`,
  };
}

export type ContainerRow = { container: string; image: string; restarts: number | null; restarts_total: number | null; ready: number | null;
  reason: string; cpu_request: number | null; cpu_limit: number | null; mem_request: number | null; mem_limit: number | null };

/** A pod's containers: restarts, readiness, waiting reason, requests and limits. */
export function k8sContainers(namespace: string, pod: string): Q {
  const last = (m: string) => `arg_max(value, ts) FILTER (WHERE metric_name = '${m}')`;
  return {
    uses: ["k8s"],
    sql: `SELECT ${R("k8s.container.name")} AS container,
  any_value(coalesce(${R("container.image.name")}, '') || CASE WHEN ${R("container.image.tag")} IS NOT NULL THEN ':' || ${R("container.image.tag")} ELSE '' END) AS image,
  max(value) FILTER (WHERE metric_name = '${K8S.restarts}') - min(value) FILTER (WHERE metric_name = '${K8S.restarts}') AS restarts,
  ${last(K8S.restarts)} AS restarts_total, ${last(K8S.containerReady)} AS ready,
  coalesce(arg_max(${A("k8s.container.status.reason")}, ts) FILTER (WHERE metric_name = '${K8S.reason}' AND value > 0), '') AS reason,
  ${last(K8S.cpuRequest)} AS cpu_request, ${last(K8S.cpuLimit)} AS cpu_limit, ${last(K8S.memRequest)} AS mem_request, ${last(K8S.memLimit)} AS mem_limit
FROM metrics WHERE metric_name IN (${list([K8S.restarts, K8S.containerReady, K8S.reason, K8S.cpuRequest, K8S.cpuLimit, K8S.memRequest, K8S.memLimit])})
  AND ${R("k8s.namespace.name")} = ${q(namespace)} AND ${R("k8s.pod.name")} = ${q(pod)}
GROUP BY 1 ORDER BY 1`,
  };
}

/** Every query with sample parameters: what the contract test runs. */
export function all(): Record<string, Q> {
  const f = {}, day = 1440;
  return {
    discovery: discovery(), clusters: clusters(),
    gpuFleet: gpuFleet(day, f), gpuFleetHour: gpuFleet(60, f), gpuSeries: gpuSeries(day, f), gpuWorkloads: gpuWorkloads(day, f),
    gpuHealth: gpuHealth(f), gpuXids: gpuXids(f), gpuStalled: gpuStalled(f), gpuDays: gpuDays(day * 7, f),
    gpuDetail: gpuDetail("GPU-x", 300),
    k8sNodes: k8sNodes(f), k8sPods: k8sPods(f), k8sPodsNs: k8sPods({ namespace: "shop" }), k8sWorkloads: k8sWorkloads(f),
    k8sEvents: k8sEvents(f), k8sUsageNs: k8sUsageSeries(300, "namespace", f), k8sUsageNode: k8sUsageSeries(300, "node", f),
    k8sPodSeries: k8sPodSeries("shop", "x", 60), k8sContainers: k8sContainers("shop", "x"),
  };
}
