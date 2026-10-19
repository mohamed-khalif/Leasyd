#!/usr/bin/env bash
# eBPF lab: a real Kubernetes node running a shop with no OpenTelemetry in it, plus Leasyd for
# Kubernetes as a customer installs it (eBPF agent on), to prove the zero-code APM end to end and to
# capture what the eBPF agent really sends for the tests (services/compaction/testdata/ebpf-capture.json.gz).
#
#   infra/ebpf-lab.sh up [HOURS=2] [TYPE=t3.large]   tenant qa-ebpflab + one instance (Ubuntu 24.04, k3s,
#                                                    leasyd-k8s.sh, workloads)
#   infra/ebpf-lab.sh status                         instance, and its bootstrap log lines in Leasyd
#   infra/ebpf-lab.sh capture [MINUTES=20]           the tenant's raw OTLP of the last minutes -> testdata
#   infra/ebpf-lab.sh down                           terminate the instance, delete the tenant
#
# Needs the deployer policy infra/iam/deployer-gpulab.json (instances tagged project=obs-gpulab, t3.*
# allowed). t3.large: about US$0.08 an hour; the instance terminates itself after HOURS. The ingest
# key lives in $LAB_STATE (outside the repo) and in the instance's user data until `down`.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:=us-east-1}"
export AWS_DEFAULT_REGION
TENANT=qa-ebpflab
LAB_STATE="${LAB_STATE:-${TMPDIR:-/tmp}/leasyd-ebpflab.json}"
ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
BUCKET="$(aws lambda get-function-configuration --function-name obs-recent-indexer --query Environment.Variables.BUCKET --output text)"

CBF=(); aws --version 2>&1 | grep -q "aws-cli/2" && CBF=(--cli-binary-format raw-in-base64-out)   # AWS CLI v2 (v1 sends JSON as is)
admin() { local out; out="$(mktemp)"; aws lambda invoke --function-name obs-tenant-admin "${CBF[@]}" --payload "$1" "$out" >/dev/null; cat "$out"; rm -f "$out"; }
state() { python3 -c "import json,sys; print(json.load(open('$LAB_STATE')).get('$1',''))"; }
instance() { aws ec2 describe-instances --filters Name=tag:project,Values=obs-gpulab Name=tag:Name,Values=obs-ebpflab Name=instance-state-name,Values=pending,running,stopping \
  --query 'Reservations[].Instances[].InstanceId' --output text; }

case "${1:-}" in
up)
  HOURS="${2:-2}" TYPE="${3:-t3.large}"
  [[ "$TENANT" == qa-* ]]
  [[ -z "$(instance)" ]] || { echo "a lab instance already runs: $(instance)"; exit 1; }
  if [[ ! -f "$LAB_STATE" ]]; then
    out="$(admin "{\"action\":\"create\",\"tenant\":\"$TENANT\",\"plan\":\"free\"}")"
    if [[ "$out" == *"already exists"* ]]; then   # made from another machine: a new key, the old one ends now
      out="$(admin "{\"action\":\"rotate\",\"tenant\":\"$TENANT\",\"grace_hours\":0}")"
    fi
    python3 -c "import json,sys; r=json.loads(sys.argv[1]); assert r.get('api_key'), r; json.dump({'tenant':'$TENANT','key':r['api_key']}, open('$LAB_STATE','w'))" "$out"
    chmod 600 "$LAB_STATE"
    admin "{\"action\":\"set-search\",\"tenant\":\"$TENANT\",\"units_per_day\":0}" >/dev/null
    admin "{\"action\":\"set-cap\",\"tenant\":\"$TENANT\",\"daily_gb\":2}" >/dev/null
    echo "tenant $TENANT created (a new key takes ~2 minutes to work; the bootstrap waits longer than that)"
  fi
  files="$(tar cz -C examples/kubernetes leasyd-k8s.sh agent.yaml cluster.yaml dcgm-exporter.yaml dcgm-counters.csv ebpf.yaml \
           -C ../../infra/ebpf-lab workloads.yaml | base64 -w0)"
  ud="$(mktemp)"
  KEY="$(state key)" python3 - "$ud" "$files" "$HOURS" "$ENDPOINT" <<'PY'
import gzip, os, sys
out, files, hours, endpoint = sys.argv[1:]
s = open("infra/ebpf-lab/bootstrap.sh").read()
s = s.replace("__KEY__", os.environ["KEY"]).replace("__ENDPOINT__", endpoint).replace("__HOURS__", hours).replace("__FILES__", files)
open(out, "wb").write(gzip.compress(s.encode()))
print(f"user data {os.path.getsize(out)} bytes (gzip)")
PY
  ami="$(aws ec2 describe-images --owners 099720109477 --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' \
         --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
  id="$(aws ec2 run-instances --image-id "$ami" --instance-type "$TYPE" --count 1 --user-data "fileb://$ud" \
        --instance-initiated-shutdown-behavior terminate \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
        --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2' \
        --tag-specifications 'ResourceType=instance,Tags=[{Key=project,Value=obs-gpulab},{Key=Name,Value=obs-ebpflab}]' \
                             'ResourceType=volume,Tags=[{Key=project,Value=obs-gpulab}]' \
        --query 'Instances[0].InstanceId' --output text)"
  rm -f "$ud"
  echo "launched $id ($TYPE, $ami); it terminates itself after $HOURS h. Progress: infra/ebpf-lab.sh status"
  ;;
status)
  id="$(instance)"; echo "instance: ${id:-none}"
  [[ -n "$id" ]] && aws ec2 describe-instances --instance-ids $id --query 'Reservations[0].Instances[0].[State.Name,LaunchTime,InstanceType]' --output text
  end="$(date -u +%Y-%m-%dT%H:%M:%SZ)" start="$(date -u -d '-3 hours' +%Y-%m-%dT%H:%M:%SZ)"
  out="$(mktemp)"
  aws lambda invoke --function-name obs-query "${CBF[@]}" --payload \
    "{\"tenant\":\"$TENANT\",\"sql\":\"SELECT ts, body FROM logs WHERE service = 'ebpflab-bootstrap' ORDER BY ts DESC LIMIT 30\",\"start\":\"$start\",\"end\":\"$end\"}" "$out" >/dev/null
  python3 -c "import json; r=json.load(open('$out')); [print(row[0][11:19], row[1][:400]) for row in reversed(r.get('rows', []))] or print(r)"
  rm -f "$out"
  ;;
capture)
  MIN="${2:-20}"
  python3 infra/ebpf-lab/capture.py "$BUCKET" "$TENANT" "$MIN" services/compaction/testdata/ebpf-capture.json.gz
  ;;
down)
  id="$(instance)"; [[ -n "$id" ]] && aws ec2 terminate-instances --instance-ids $id >/dev/null && echo "terminating $id"
  [[ "$TENANT" == qa-* ]] && admin "{\"action\":\"delete\",\"tenant\":\"$TENANT\"}" && echo && rm -f "$LAB_STATE"
  ;;
*) sed -n '2,16p' "$0"; exit 2 ;;
esac
