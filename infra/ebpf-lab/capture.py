"""Capture what the eBPF lab's agents really sent (the tenant's raw OTLP files of the last MINUTES)
as a test fixture: {"captured": iso, "files": [{"signal", "dt", "hour", "docs": [...]}]}, gzip.
Keeps the eBPF agent's traces and metrics (the otlp/ebpf pipelines), not the cluster's kubelet
stats or the lab's own bootstrap logs; at most MAX_TRACE_DOCS trace documents.

    python3 infra/ebpf-lab/capture.py BUCKET TENANT MINUTES OUT.json.gz
"""
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3

MAX_TRACE_DOCS = 400


def ebpf(resource_items):
    """The eBPF agent's resources: telemetry.distro.name opentelemetry-ebpf-instrumentation."""
    out = []
    for r in resource_items:
        attrs = {a["key"]: next(iter(a["value"].values()), "") for a in (r.get("resource") or {}).get("attributes", [])}
        if attrs.get("telemetry.distro.name") == "opentelemetry-ebpf-instrumentation":
            out.append(r)
    return out


def main(bucket, tenant, minutes, out_path):
    assert tenant.startswith("qa-")
    s3 = boto3.client("s3")
    since = datetime.now(timezone.utc) - timedelta(minutes=int(minutes))
    files, distros, trace_docs = [], set(), 0
    for signal, key in (("metrics", "resourceMetrics"), ("traces", "resourceSpans")):
        for day in sorted({since.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")}):
            prefix = f"_incoming/tenant={tenant}/{signal}/dt={day}/"
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if obj["LastModified"] < since or (signal == "traces" and trace_docs >= MAX_TRACE_DOCS):
                        continue
                    hour = obj["Key"].split("/hour=")[1].split("/")[0]
                    body = gzip.decompress(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()).decode()
                    docs = []
                    for line in body.splitlines():
                        if line.strip():
                            items = json.loads(line).get(key, [])
                            for r in items:
                                for a in (r.get("resource") or {}).get("attributes", []):
                                    if a["key"] == "telemetry.distro.name":
                                        distros.add(next(iter(a["value"].values()), ""))
                            items = ebpf(items)
                            if items:
                                docs.append({key: items})
                    if docs:
                        trace_docs += len(docs) if signal == "traces" else 0
                        files.append({"signal": signal, "dt": day, "hour": hour, "docs": docs})
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    data = {"captured": datetime.now(timezone.utc).isoformat(), "tenant": tenant, "minutes": int(minutes), "files": files}
    with gzip.open(out_path, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    n = sum(len(f["docs"]) for f in files)
    print(f"telemetry.distro.name seen: {sorted(distros)}")
    print(f"{len(files)} files, {n} documents -> {out_path} ({os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    main(*sys.argv[1:5])
