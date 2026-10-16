# Leasyd for Kubernetes (and NVIDIA GPUs)

```
LEASYD_API_KEY=obs_... bash <(curl -fsSL https://app.leasyd.com/install/kubernetes/leasyd-k8s.sh) --cluster prod [--gpu]
```

| File | What |
|---|---|
| `leasyd-k8s.sh` | Installer: Secret with the key, then the Helm releases below (namespace `leasyd`) |
| `agent.yaml` | Values for `open-telemetry/opentelemetry-collector` as a DaemonSet: kubelet stats, pod logs, GPU metrics (scrapes dcgm-exporter on its node), apps' OTLP on each node's 4317/4318 |
| `cluster.yaml` | Values for the same chart as one Deployment: `k8s_cluster` (pod phases, restarts, workloads, node allocatable and conditions) and `k8s_events` |
| `dcgm-exporter.yaml` | Values for NVIDIA's dcgm-exporter chart (`--gpu`, when the GPU Operator doesn't already run it) |
| `dcgm-counters.csv` | The DCGM fields: NVIDIA's defaults plus ECC errors, throttling time and SM activity |

Permissions (read only): the chart's presets (pods, namespaces, nodes, workloads, events, node
stats) plus `nodes/pods` and `nodes/proxy` (get) for the kubelet's pod list; Kubernetes 1.33+ needs
only `nodes/pods`, so `nodes/proxy` can be removed from `agent.yaml` there.

What Leasyd reads from this data, and why: `docs/gpu-kubernetes.md`. Checked with
`helm template` (chart 0.176.0) and `otelcol-contrib validate` (0.162.0); end to end on a real
cluster with `infra/gpu-lab.sh`.
