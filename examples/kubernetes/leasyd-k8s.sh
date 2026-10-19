#!/usr/bin/env bash
# Leasyd for Kubernetes: installs the OpenTelemetry Collector in your cluster so Leasyd shows its
# nodes, workloads, pods, events and logs, its NVIDIA GPUs (usage, health, cost) when it has some,
# and, with eBPF, every app's requests, errors, latency, connections and traces, with no code change.
#
#   LEASYD_API_KEY=obs_... bash leasyd-k8s.sh --cluster prod-eu [--gpu] [--no-logs]
#                                             [--ebpf-traces PERCENT|off] [--no-ebpf]
#   bash leasyd-k8s.sh --uninstall
#
# What it installs, in namespace leasyd (Helm releases, official charts):
#   leasyd-agent    one collector per node (DaemonSet): kubelet stats, pod logs, GPU metrics, and
#                   your apps' OTLP on each node's port 4317 / 4318          (values: agent.yaml)
#   leasyd-cluster  one collector (Deployment): the Kubernetes API's state and events (cluster.yaml)
#   leasyd-dcgm     with --gpu, only if no dcgm-exporter runs yet: NVIDIA's exporter on the GPU
#                   nodes (dcgm-exporter.yaml, fields in dcgm-counters.csv)
#   leasyd-ebpf     unless --no-ebpf: OpenTelemetry eBPF Instrumentation, one per node (ebpf.yaml).
#                   Traces kept: all with an error, all slower than 1 s, and PERCENT % of the rest
#                   (--ebpf-traces, default 10; off: none, metrics only)
# The API key goes into the Secret leasyd/leasyd-api-key, never into Helm values.
# Needs kubectl (pointing at the cluster) and helm 3. Safe to re-run (to change the key too).
set -euo pipefail
BASE="${LEASYD_INSTALL_BASE:-https://app.leasyd.com/install/kubernetes}"
ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
NS=leasyd
OTEL_CHART_VERSION="${OTEL_CHART_VERSION:-0.176.0}"
OBI_VERSION="${OBI_VERSION:-v0.14.0}"
CLUSTER="" GPU=false LOGS=true UNINSTALL=false EBPF=true EBPF_TRACES=10
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster) CLUSTER="${2:?--cluster needs a name}"; shift 2 ;;
    --gpu) GPU=true; shift ;;
    --no-logs) LOGS=false; shift ;;
    --no-ebpf) EBPF=false; shift ;;
    --ebpf-traces) EBPF_TRACES="${2:?--ebpf-traces needs a percentage or off}"; shift 2 ;;
    --uninstall) UNINSTALL=true; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
for tool in kubectl helm; do
  command -v "$tool" >/dev/null || { echo "$tool is needed" >&2; exit 1; }
done

if $UNINSTALL; then
  for r in leasyd-agent leasyd-cluster leasyd-dcgm; do helm uninstall "$r" -n "$NS" 2>/dev/null || true; done
  kubectl delete clusterrolebinding,clusterrole leasyd-ebpf --ignore-not-found >/dev/null
  kubectl delete namespace "$NS" --ignore-not-found
  echo "Leasyd removed from this cluster"
  exit 0
fi
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
[[ -n "$CLUSTER" ]] || CLUSTER="$(kubectl config current-context | sed 's#.*/##')"
[[ "$CLUSTER" =~ ^[A-Za-z0-9._-]{1,63}$ ]] || { echo "cluster name: letters, digits, . _ - (up to 63)" >&2; exit 2; }
if [[ "$EBPF_TRACES" == off ]]; then SAMPLER=always_off EBPF_TRACES=0
elif [[ "$EBPF_TRACES" =~ ^([0-9]|[1-9][0-9]|100)$ ]]; then SAMPLER=always_on
else echo "--ebpf-traces: a percentage 0-100, or off" >&2; exit 2; fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
here="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
fetch() {   # the values files: next to this script when run from a checkout, else from Leasyd
  if [[ -n "$here" && -f "$here/$1" ]]; then cp "$here/$1" "$tmp/$1"; else curl -fsSL "$BASE/$1" -o "$tmp/$1"; fi
}
for f in agent.yaml cluster.yaml dcgm-exporter.yaml dcgm-counters.csv ebpf.yaml; do fetch "$f"; done

echo "== namespace $NS and the API key Secret"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# The eBPF agent runs privileged: let this namespace (and only it) do that under Pod Security
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=privileged --overwrite >/dev/null
kubectl -n "$NS" create secret generic leasyd-api-key --from-literal=api-key="$LEASYD_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update open-telemetry >/dev/null

if $GPU; then
  # GPU nodes: the ones that offer nvidia.com/gpu (needs NVIDIA's device plugin or GPU Operator)
  gpu_nodes="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' \
               | awk '$2 > 0 {print $1}')"
  if [[ -z "$gpu_nodes" ]]; then
    echo "!! no node offers nvidia.com/gpu yet: install NVIDIA's device plugin or GPU Operator first" >&2
  fi
  for n in $gpu_nodes; do kubectl label node "$n" leasyd.com/gpu=true --overwrite >/dev/null; done
  dcgm_running() {
    [[ -n "$(kubectl get pods -A -l app=nvidia-dcgm-exporter -o name 2>/dev/null)" ||
       -n "$(kubectl get pods -A -l app.kubernetes.io/name=dcgm-exporter -o name 2>/dev/null)" ]]
  }
  if dcgm_running && ! helm status leasyd-dcgm -n "$NS" >/dev/null 2>&1; then
    echo "== dcgm-exporter already runs (GPU Operator?): Leasyd's agent scrapes it"
  else
    echo "== NVIDIA dcgm-exporter on the GPU nodes ($(echo $gpu_nodes | wc -w) found)"
    helm repo add nvidia-dcgm https://nvidia.github.io/dcgm-exporter/helm-charts >/dev/null 2>&1 || true
    helm repo update nvidia-dcgm >/dev/null
    runtime=(); kubectl get runtimeclass nvidia >/dev/null 2>&1 && runtime=(--set runtimeClassName=nvidia)
    helm upgrade --install leasyd-dcgm nvidia-dcgm/dcgm-exporter -n "$NS" -f "$tmp/dcgm-exporter.yaml" \
      --set-file customMetrics="$tmp/dcgm-counters.csv" ${runtime[@]+"${runtime[@]}"} --wait --timeout 5m >/dev/null
  fi
fi

# The whole extraEnvs list: helm --set on one list item would drop the others
cat > "$tmp/env.yaml" <<YAML
extraEnvs:
  - name: LEASYD_API_KEY
    valueFrom: { secretKeyRef: { name: leasyd-api-key, key: api-key } }
  - { name: LEASYD_ENDPOINT, value: "$ENDPOINT" }
  - { name: LEASYD_CLUSTER, value: "$CLUSTER" }
  - { name: LEASYD_EBPF_TRACES_PERCENT, value: "$EBPF_TRACES" }
YAML
common=(-f "$tmp/env.yaml")
echo "== leasyd-agent (one collector per node)"
helm upgrade --install leasyd-agent open-telemetry/opentelemetry-collector --version "$OTEL_CHART_VERSION" -n "$NS" \
  -f "$tmp/agent.yaml" "${common[@]}" --set "presets.logsCollection.enabled=$LOGS" --wait --timeout 5m >/dev/null
echo "== leasyd-cluster (the cluster's state and events)"
helm upgrade --install leasyd-cluster open-telemetry/opentelemetry-collector --version "$OTEL_CHART_VERSION" -n "$NS" \
  -f "$tmp/cluster.yaml" "${common[@]}" --wait --timeout 5m >/dev/null

if $EBPF; then
  echo "== leasyd-ebpf (OpenTelemetry eBPF Instrumentation $OBI_VERSION, one per node)"
  sed -e "s/__OBI_VERSION__/$OBI_VERSION/" -e "s/__CLUSTER__/$CLUSTER/" -e "s/__TRACES_SAMPLER__/$SAMPLER/" \
      -e "s/__CONFIG_HASH__/$(cat "$tmp/ebpf.yaml" | sha256sum | cut -c1-12)-$CLUSTER-$SAMPLER/" "$tmp/ebpf.yaml" > "$tmp/ebpf.out.yaml"
  kubectl apply -f "$tmp/ebpf.out.yaml" >/dev/null
  kubectl -n "$NS" rollout status daemonset/leasyd-ebpf --timeout 5m >/dev/null ||
    echo "!! the eBPF agent didn't start on every node: kubectl -n $NS describe pods -l app.kubernetes.io/name=leasyd-ebpf" >&2
else
  kubectl delete -f "$tmp/ebpf.yaml" --ignore-not-found >/dev/null 2>&1 || true
fi

kubectl -n "$NS" get pods -o wide
cat <<EOF

Leasyd now receives cluster "$CLUSTER". Within a minute or two: Leasyd > Kubernetes$($GPU && echo " and GPUs")$($EBPF && echo ",
and every app in the cluster on Leasyd > Service map (eBPF: no code changes)").
Apps in the cluster can send OpenTelemetry to their node's agent:
  OTEL_EXPORTER_OTLP_ENDPOINT=http://\$(NODE_IP):4318   (NODE_IP from the downward API: status.hostIP)
Remove everything: bash leasyd-k8s.sh --uninstall
EOF
