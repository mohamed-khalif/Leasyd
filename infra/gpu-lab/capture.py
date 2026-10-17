"""Capture what the GPU lab's collectors really sent (the tenant's raw OTLP files of the last
MINUTES) as a test fixture: {"captured": iso, "files": [{"signal", "dt", "hour", "docs": [...]}]},
gzip. Keeps only Kubernetes and GPU data (kubeletstats, k8s_cluster, k8s_events, the dcgm-exporter
scrape), not the lab's own bootstrap logs.

    python3 infra/gpu-lab/capture.py BUCKET TENANT MINUTES OUT.json.gz
"""
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3

KEEP_SCOPES = ("kubeletstats", "k8scluster", "k8sevents", "k8s_events", "prometheus")


def keep(resource_items):
    out = []
    for r in resource_items:
        scopes = r.get("scopeMetrics") or r.get("scopeLogs") or []
        names = [(s.get("scope") or {}).get("name", "") for s in scopes]
        if any(k in n for n in names for k in KEEP_SCOPES):
            out.append(r)
    return out


def main(bucket, tenant, minutes, out_path):
    assert tenant.startswith("qa-")
    s3 = boto3.client("s3")
    since = datetime.now(timezone.utc) - timedelta(minutes=int(minutes))
    files = []
    for signal, key in (("metrics", "resourceMetrics"), ("logs", "resourceLogs")):
        for day in sorted({since.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")}):
            prefix = f"_incoming/tenant={tenant}/{signal}/dt={day}/"
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if obj["LastModified"] < since:
                        continue
                    hour = obj["Key"].split("/hour=")[1].split("/")[0]
                    body = gzip.decompress(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()).decode()
                    docs = []
                    for line in body.splitlines():
                        if line.strip():
                            items = keep(json.loads(line).get(key, []))
                            if items:
                                docs.append({key: items})
                    if docs:
                        files.append({"signal": signal, "dt": day, "hour": hour, "docs": docs})
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    data = {"captured": datetime.now(timezone.utc).isoformat(), "tenant": tenant, "minutes": int(minutes), "files": files}
    with gzip.open(out_path, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    n = sum(len(f["docs"]) for f in files)
    print(f"{len(files)} files, {n} documents -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
