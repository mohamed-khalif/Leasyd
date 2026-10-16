#!/bin/bash
# GPU lab instance bootstrap (user data; infra/gpu-lab.sh fills in the __PLACEHOLDERS__). On a
# Deep Learning Base GPU AMI (NVIDIA driver, Docker, NVIDIA container toolkit): k3s, NVIDIA's
# device plugin, then Leasyd for Kubernetes exactly as a customer installs it (leasyd-k8s.sh
# --gpu), then workloads.yaml. Progress goes to Leasyd itself as logs of service gpulab-bootstrap.
# The instance stops (and so terminates) itself after __HOURS__ hours.
exec > >(tee -a /var/log/gpulab.log) 2>&1
# no `set -x`: it would print the ingest key to the console log (EC2 GetConsoleOutput)
KEY='__KEY__'
ENDPOINT='__ENDPOINT__'
CLUSTER=gpu-lab
shutdown -h "+$((__HOURS__ * 60))" || true

log() {
  echo "== $*"
  local body; body=$(printf '%s' "$*" | head -c 3000 | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  curl -s -m 10 -X POST "$ENDPOINT/v1/logs" -H "x-api-key: $KEY" -H 'Content-Type: application/json' -d "{\"resourceLogs\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"gpulab-bootstrap\"}}]},\"scopeLogs\":[{\"logRecords\":[{\"timeUnixNano\":\"$(date +%s%N)\",\"severityText\":\"INFO\",\"body\":{\"stringValue\":$body}}]}]}]}" >/dev/null || true
}
trap 'log "FAILED at line $LINENO: $BASH_COMMAND"' ERR
set -eE

log "start: $(nvidia-smi -L | tr '\n' ' ')"
mkdir -p /opt/leasyd && cd /opt/leasyd
echo '__FILES__' | base64 -d | tar xz

# NVIDIA as the default runtime, so the device plugin and dcgm-exporter see the GPUs whatever
# their runtimeClassName (k3s finds nvidia-container-runtime, which the AMI has).
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --default-runtime nvidia" sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 5; done
log "k3s ready: $(kubectl get nodes -o wide | tail -1)"

curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
kubectl get runtimeclass nvidia >/dev/null 2>&1 || kubectl apply -f - <<'YAML'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: { name: nvidia }
handler: nvidia
YAML
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update nvdp
# The chart's DaemonSet only runs on nodes labelled as having NVIDIA GPUs (NFD / GPU Operator labels);
# a bare k3s node has none, so say it here.
kubectl label node --all nvidia.com/gpu.present=true --overwrite
helm upgrade --install nvdp nvdp/nvidia-device-plugin -n nvidia-device-plugin --create-namespace --set runtimeClassName=nvidia --wait --timeout 10m
for i in $(seq 1 60); do
  [ "$(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}')" -ge 1 ] 2>/dev/null && break
  sleep 5
done
if ! [ "$(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}')" -ge 1 ] 2>/dev/null; then
  log "no GPUs allocatable after 5 min. containerd: $(grep -A3 -i nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml | head -20 | tr '\n' ' ')"
  log "device plugin: $(kubectl -n nvidia-device-plugin logs -l app.kubernetes.io/name=nvidia-device-plugin --tail=30 2>&1 | tr '\n' ' ')"
  log "which: $(ls -l /usr/bin/nvidia-container-runtime* 2>&1 | tr '\n' ' ') daemonsets: $(kubectl get ds -A --no-headers 2>&1 | tr '\n' ';')"
  exit 1
fi
log "device plugin ready: $(kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}') GPUs allocatable"

LEASYD_API_KEY="$KEY" LEASYD_ENDPOINT="$ENDPOINT" bash /opt/leasyd/leasyd-k8s.sh --cluster "$CLUSTER" --gpu
log "leasyd installed: $(kubectl -n leasyd get pods --no-headers | awk '{print $1" "$3}' | tr '\n' ';')"

kubectl apply -f /opt/leasyd/workloads.yaml
log "workloads applied"

set +eE
trap - ERR
for i in $(seq 1 12); do      # the first hour: what runs, and the collectors' own complaints
  sleep 300
  log "pods: $(kubectl get pods -A --no-headers | awk '{print $1"/"$2" "$4" "$5}' | tr '\n' ';')"
  for p in $(kubectl -n leasyd get pods -o name); do
    log "$p: $(kubectl -n leasyd logs "$p" --tail=40 2>&1 | grep -iE 'error|warn|fail' | tail -8 | tr '\n' ' ')"
  done
done
log "done watching"
