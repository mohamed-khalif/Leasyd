"""One-off after deploying summaries (docs/summaries.md): builds them for hours compacted before they existed,
for every active account, the last 31 days. Safe to re-run: each hour is rebuilt, not added to.

    AWS_DEFAULT_REGION=us-east-1 python3 infra/backfill_summaries.py [ACCOUNT ...]
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

lam = boto3.client("lambda", config=botocore.config.Config(read_timeout=900, retries={"max_attempts": 0}))


def accounts():
    out = subprocess.run(["infra/tenant.sh", "list"], capture_output=True, text=True, check=True).stdout
    return [line.split()[0] for line in out.splitlines() if len(line.split()) > 1 and line.split()[1] == "active"]


def day(tenant, d):
    r = lam.invoke(FunctionName="obs-compaction-worker",
                   Payload=json.dumps({"summaries_backfill": {"tenant": tenant, "day": d}}).encode())
    out = json.loads(r["Payload"].read() or b"{}")
    if r.get("FunctionError"):
        return f"{tenant} {d}: FAILED {out.get('errorMessage', out)}"
    return f"{tenant} {d}: {len(out.get('written', []))} files"


today = datetime.now(timezone.utc).date()
jobs = [(t, str(today - timedelta(days=i))) for t in (sys.argv[1:] or accounts()) for i in range(31)]
with ThreadPoolExecutor(8) as pool:
    for line in pool.map(lambda j: day(*j), jobs):
        print(line, flush=True)
