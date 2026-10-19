#!/bin/bash
# eBPF lab instance bootstrap (user data; infra/ebpf-lab.sh fills in the __PLACEHOLDERS__). On plain
# Ubuntu 24.04: k3s, then Leasyd for Kubernetes exactly as a customer installs it (leasyd-k8s.sh,
# eBPF on by default), then workloads.yaml (a shop with no OpenTelemetry in it). Progress goes to
# Leasyd itself as logs of service ebpflab-bootstrap. The instance stops (so terminates) after
# __HOURS__ hours.
exec > >(tee -a /var/log/ebpflab.log) 2>&1
# no `set -x`: it would print the ingest key to the console log (EC2 GetConsoleOutput)
KEY='__KEY__'
ENDPOINT='__ENDPOINT__'
CLUSTER=ebpf-lab
shutdown -h "+$((__HOURS__ * 60))" || true

log() {
  echo "== $*"
  local body; body=$(printf '%s' "$*" | head -c 3000 | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  curl -s -m 10 -X POST "$ENDPOINT/v1/logs" -H "x-api-key: $KEY" -H 'Content-Type: application/json' -d "{\"resourceLogs\":[{\"resource\":{\"attributes\":[{\"key\":\"service.name\",\"value\":{\"stringValue\":\"ebpflab-bootstrap\"}}]},\"scopeLogs\":[{\"logRecords\":[{\"timeUnixNano\":\"$(date +%s%N)\",\"severityText\":\"INFO\",\"body\":{\"stringValue\":$body}}]}]}]}" >/dev/null || true
}
trap 'log "FAILED at line $LINENO: $BASH_COMMAND"' ERR
set -eE

log "start: kernel $(uname -r), BTF $(test -f /sys/kernel/btf/vmlinux && echo yes || echo no)"
mkdir -p /opt/leasyd && cd /opt/leasyd
echo '__FILES__' | base64 -d | tar xz

curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644" sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 5; done
log "k3s ready: $(kubectl get nodes -o wide | tail -1)"
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

LEASYD_API_KEY="$KEY" LEASYD_ENDPOINT="$ENDPOINT" bash /opt/leasyd/leasyd-k8s.sh --cluster "$CLUSTER"
log "leasyd installed: $(kubectl -n leasyd get pods --no-headers | awk '{print $1" "$3}' | tr '\n' ';')"

kubectl apply -f /opt/leasyd/workloads.yaml
log "workloads applied"

set +eE
trap - ERR
for i in $(seq 1 8); do      # the first 40 minutes: what runs, and the agents' own complaints
  sleep 300
  log "pods: $(kubectl get pods -A --no-headers | awk '{print $1"/"$2" "$4" "$5}' | tr '\n' ';')"
  log "ebpf agent: $(kubectl -n leasyd logs -l app.kubernetes.io/name=leasyd-ebpf --tail=200 2>&1 | grep -iE 'error|warn|instrumenting|new process' | grep -v IMDS | tail -12 | tr '\n' ' ')"
  log "collector: $(kubectl -n leasyd logs -l app.kubernetes.io/instance=leasyd-agent --tail=200 2>&1 | grep -iE 'error|warn|fail' | tail -6 | tr '\n' ' ')"
done
log "done watching"
