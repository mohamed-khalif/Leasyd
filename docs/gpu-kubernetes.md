# Kubernetes and GPU monitoring: design

Goal: Datadog-level Kubernetes and NVIDIA GPU monitoring (fleet health, usage, cost, waste, stalled
workloads, hardware issues, planning) built only on standard OpenTelemetry data, so it works with
any cluster (EKS, GKE, AKS, on-prem, k3s) and with GPUs outside Kubernetes (plain hosts, Slurm).

Everything below is a contract. The pages, built-in dashboards, alert templates, the AI SRE and
the demo generator all read the same names, from one place each:

| What | Source of truth |
|---|---|
| Metric / attribute names, thresholds, XID table, prices | `services/web/src/infra/model.ts` |
| Every SQL the pages send | `services/web/src/infra/queries.ts` (pure functions, no React) |
| What collectors send | `examples/kubernetes/` (Helm values + installer), `examples/ec2/setup.sh --gpu` |
| Demo data (same shapes) | `services/demo/cluster.py` |
| Proof the queries work | `services/compaction/test_infra_queries.py`: runs every query in `queries.ts` on the real engine over a real captured payload (`testdata/k8s-gpu-capture.json.gz`) and the demo data |

## 1. What is collected

Installer: `leasyd-k8s.sh` (served at `/install/leasyd-k8s.sh`) runs two releases of the official
`open-telemetry/opentelemetry-collector` Helm chart (contrib image) in namespace `leasyd`, with
the API key in a Secret and the cluster name as `k8s.cluster.name` on everything.

**Agent (DaemonSet, one per node)**
- `otlp`: apps on the node send to the agent (hostPort 4317/4318); `k8sattributes` adds pod,
  namespace, node, workload (deployment / statefulset / daemonset / job / cronjob) and the pod
  labels `team` and `app.kubernetes.io/name`.
- `kubeletstats` (node, pod, container): `k8s.node.cpu.usage`, `k8s.node.memory.working_set`,
  `k8s.node.memory.available`, `k8s.node.filesystem.usage/capacity`, `k8s.node.network.io`,
  `k8s.pod.cpu.usage`, `k8s.pod.memory.working_set`, `k8s.pod.network.io`, plus
  `k8s.pod.cpu_limit_utilization`, `k8s.pod.memory_limit_utilization` (and request variants),
  `container.cpu.usage`, `container.memory.working_set`.
- `filelog` (pod logs), with `service.name` from the `app.kubernetes.io/name` label, else the
  workload name, else the container name. `--no-logs` turns it off.
- `prometheus` scraping NVIDIA's dcgm-exporter pods **on the same node** (labels
  `app=nvidia-dcgm-exporter` from the GPU Operator or `app.kubernetes.io/name=dcgm-exporter` from
  its Helm chart), job `dcgm-exporter`, the node name added as label `node`.

**Cluster (Deployment, one replica)**
- `k8s_cluster`: `k8s.pod.phase` (1 Pending, 2 Running, 3 Succeeded, 4 Failed, 5 Unknown),
  `k8s.container.restarts`, `k8s.container.ready`, `k8s.container.status.reason`
  (CrashLoopBackOff, ImagePullBackOff, OOMKilled...), cpu/memory requests and limits,
  `k8s.deployment.desired/available`, statefulset / daemonset / job counts,
  `k8s.node.allocatable_cpu/memory/pods`, the node condition Ready.
- `k8s_events`: Kubernetes events as logs: body = message, severity WARN for Warning events,
  attributes `k8s.event.reason`, `k8s.namespace.name`; resource `k8s.object.kind`,
  `k8s.object.name`.

`service.name`: `kubernetes` for kubelet, cluster and event data (unless something set one),
`dcgm-exporter` for GPU metrics. Pages never require these (they match metric names), but use them
as a hint to read fewer files (see 4).

**GPUs outside Kubernetes**: `setup.sh --gpu` (the Linux server collector) runs dcgm-exporter in
Docker and scrapes `localhost:9400`. Slurm: dcgm-exporter's `--hpc-job-mapping-dir` adds
`hpc_job`, which Leasyd treats as the workload.

**DCGM metrics used** (dcgm-exporter default counters plus Leasyd's `dcgm-counters.csv`, which adds
ECC, throttle reasons and power violation):

| Use | Metric |
|---|---|
| Utilization (always) | `DCGM_FI_DEV_GPU_UTIL` (%) |
| Utilization (when profiling is available) | `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, `DCGM_FI_PROF_SM_ACTIVE`, `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` (0-1) |
| Memory | `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE` (MiB) |
| Temperature, power | `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_DEV_MEMORY_TEMP` (C), `DCGM_FI_DEV_POWER_USAGE` (W) |
| Hardware errors | `DCGM_FI_DEV_XID_ERRORS` (last XID), `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`, `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`, `DCGM_FI_DEV_ROW_REMAP_FAILURE`, `DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS`, `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` |
| Throttling | `DCGM_FI_DEV_CLOCKS_EVENT_REASONS` (older DCGM: `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`), bitmask |

Attributes on every DCGM point: `UUID`, `gpu` (index), `modelName`, `Hostname`, `pci_bus_id`,
`DCGM_FI_DRIVER_VERSION`; and when a pod holds the GPU, `namespace`, `pod`, `container`.

## 2. Identity and attribution

- **GPU** = `UUID` (else `Hostname/gpu`). **Node** = `node` (agent relabel), else `Hostname`.
- **Workload** of a GPU = its `namespace/pod` (Kubernetes), else `hpc_job` (Slurm), else none.
- **Owner** of a pod: joined from kubelet pod metrics on (namespace, pod): workload kind and name
  (`k8s.deployment.name`, `k8s.statefulset.name`, `k8s.job.name`, ...), team = pod label `team`
  (`k8s.pod.labels.team`), else the namespace.

## 3. Usage, idle and cost

Time is cut into buckets (5 min for ranges up to 1 day, 1 h above). For each (GPU, bucket) with
data: average utilization, the pod holding it (latest in the bucket). Each such pair is one
bucket of **GPU time**. A bucket is:
- **active**: utilization >= 5 %
- **idle**: held by a workload but under 5 % (reclaimable waste)
- **unallocated**: no workload and under 5 % (spare capacity)

**Cost** = GPU hours x price per GPU hour of its model. Prices: defaults per model (AWS on-demand
list price per GPU, us-east-1, see `model.ts`), which an account's owners can replace in Settings
(`GET/PUT /v1/app/account/gpu-prices`). Costs are estimates and the UI says so.

**Planning**: per day, peak and 95th percentile of GPUs in use (active) and allocated; a linear
trend over the range projects demand 30 days out; fleet size minus peak allocated = GPUs that were
never needed (right-sizing).

**Stalled workloads**: pods Pending whose events say `FailedScheduling ... nvidia.com/gpu`
(waiting for GPUs); GPU pods with containers waiting (ContainerCreating, ImagePullBackOff,
CrashLoopBackOff); GPUs held by a pod but idle for the whole range.

**Hardware issues**: any XID (with what it means and what to do, `XID` table in `model.ts`),
double-bit ECC errors, row-remap failure, PCIe replays rising, throttling for thermal or power
reasons, temperature above 85 C.

## 4. Query budget

Every page query aggregates in SQL so results stay far below the 10,000-row limit (per GPU, per
model, per namespace, per time bucket; never per raw point). SQL reads whole files of the range;
`run_sql` takes an optional `services` hint (read only those services' files), which the pages
pass once they know which services carry Kubernetes or DCGM data, so a busy account's app metrics
are not read.

## 5. Not covered (yet)

Cluster autoscaler events beyond what Kubernetes events say; MIG partitions are shown as their
parent GPU (`GPU_I_ID` is kept as an attribute); AMD and Intel GPUs.
