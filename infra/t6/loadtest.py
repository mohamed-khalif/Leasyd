#!/usr/bin/env python3
"""T6 load tests: drive obs-loadgen, collect results and metrics, report.

  loadtest.py preflight                          account limits that cap the test
  loadtest.py tenants create [--n 100]           create t6-000.. (keys saved to --keys, outside the repo)
  loadtest.py run --gbph 1 --minutes 45 --step s1 [--yes]
  loadtest.py report --step s1                   send stats, freshness, lookup speed, platform metrics, cost/GB
  loadtest.py compaction --since-minutes 180     compaction lag and cost over a window
  loadtest.py tenants delete                     delete every t6 tenant (T5 deletion)

Tenant sizes follow a Zipf curve (rank r gets weight 1/(r+1)): a few large
tenants and a long tail, like a real SaaS. Volume is uncompressed OTLP
protobuf bytes.
"""

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
KEYS_DEFAULT = os.path.expanduser("~/.obs-t6-keys.json")
RUNS_FILE = os.path.expanduser("~/.obs-t6-runs.json")   # step -> start/end/target, for reports
RUN = "t6"
WORKER_BYTES_PER_S = 2_000_000   # what one loadgen invocation is asked to send at most
WORKER_MAX_TENANTS = 20
WINDOW_S = 840                   # loadgen invocations run <= 14 min (Lambda max is 15)
PROBE_RANKS = [0, 1, 2, 5, 10, 20, 40, 60, 80, 99]

# us-east-1 list prices, for cost per GB.
PRICE = {
    "lambda_gb_s_arm": 0.0000133334, "lambda_request": 0.20 / 1e6, "apigw_request": 3.50 / 1e6,
    "firehose_gb": 0.029, "s3_put": 0.005 / 1000, "s3_get": 0.0004 / 1000, "ddb_wru": 1.25 / 1e6,
    "ddb_rru": 0.25 / 1e6, "eventbridge_event": 1.0 / 1e6,
}
MEMORY_MB = {"obs-ingest": 1024, "obs-ingest-authorizer": 256, "obs-recent-indexer": 2048,
             "obs-compaction-worker": 3008, "obs-compaction-dispatcher": 256, "obs-index-lookup": None}

# Tenant creation waits ~1 min for streams: don't time out (and silently retry) at 60 s.
lam = boto3.client("lambda", region_name=REGION, config=botocore.config.Config(
    read_timeout=900, retries={"max_attempts": 0, "mode": "standard"}))
ddb = boto3.client("dynamodb", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)


def tenant_name(rank):
    return f"t6-{rank:03d}"


def weights(n):
    w = [1 / (r + 1) for r in range(n)]
    s = sum(w)
    return [x / s for x in w]


def endpoint():
    out = cfn.describe_stacks(StackName="obs-phaseT2")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in out if o["OutputKey"] == "IngestEndpoint")


def invoke(fn, payload, asynchronous=False):
    r = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode(),
                   InvocationType="Event" if asynchronous else "RequestResponse")
    if asynchronous:
        return None
    out = json.loads(r["Payload"].read() or b"null")
    if r.get("FunctionError") or (isinstance(out, dict) and "error" in out):
        raise RuntimeError(f"{fn}: {out}")
    return out


# ------------------------------------------------------------------ commands

def preflight(args):
    try:
        acct = lam.get_account_settings()["AccountLimit"]
        print(f"Lambda concurrency limit: {acct['ConcurrentExecutions']} "
              f"(unreserved {acct['UnreservedConcurrentExecutions']})")
        if acct["ConcurrentExecutions"] < 500:
            print("  WARNING: below 500; a 50 GB/h step needs ~200 concurrent functions. "
                  "Request an increase in Service Quotas (Lambda > Concurrent executions).")
    except Exception as e:  # noqa: BLE001
        print(f"Lambda account settings: {e}")
    try:
        sq = boto3.client("service-quotas", region_name=REGION)
        for code, name in (("L-FBD5B8FC", "Firehose delivery streams"),):
            print(f"{name}: {sq.get_service_quota(ServiceCode='firehose', QuotaCode=code)['Quota']['Value']:.0f}")
    except Exception as e:  # noqa: BLE001
        print(f"Service quotas: {e}")


def tenants_create(args):
    keys = _load_keys(args.keys, missing_ok=True)
    w = weights(args.n)

    def one(rank):
        t = tenant_name(rank)
        if t in keys:
            return t, keys[t]
        try:
            out = invoke("obs-tenant-admin", {"action": "create", "tenant": t})
        except RuntimeError as e:
            if "already exists" not in str(e):
                raise
            # Created by an earlier, interrupted run whose key was never saved: issue a new one.
            out = invoke("obs-tenant-admin", {"action": "rotate", "tenant": t, "grace_hours": 0})
        n_services = max(2, round(20 * w[rank] / w[0]))  # biggest tenant 20 services, tail 2
        return t, {"key": out["api_key"], "rank": rank, "services": [f"svc-{i:02d}" for i in range(n_services)]}

    with ThreadPoolExecutor(8) as pool:
        for t, v in pool.map(one, range(args.n)):
            keys[t] = v
            _save_keys(args.keys, keys)
            print(f"{t}: {len(v['services'])} services")
    print(f"{len(keys)} tenants; keys in {args.keys}. New keys take 1-2 minutes to activate.")


def tenants_delete(args):
    keys = _load_keys(args.keys)

    def one(t):
        try:
            return t, invoke("obs-tenant-admin", {"action": "delete", "tenant": t}).get("status")
        except RuntimeError as e:
            return t, str(e)
    with ThreadPoolExecutor(8) as pool:
        for t, st in pool.map(one, sorted(keys)):
            print(f"{t}: {st}")
    os.rename(args.keys, args.keys + ".deleted")


def plan_workers(keys, gbph):
    """Bin tenants into loadgen invocations of <= WORKER_BYTES_PER_S; a tenant
    bigger than that is split across several."""
    total = gbph * 1e9 / 3600
    w = weights(len(keys))
    parts = []
    for t, v in sorted(keys.items(), key=lambda kv: kv[1]["rank"]):
        rate = total * w[v["rank"]]
        n = max(1, -(-int(rate) // WORKER_BYTES_PER_S))
        parts += [{"tenant": t, "key": v["key"], "bytes_per_s": rate / n, "services": v["services"]}] * n
    workers, cur, cur_rate = [], [], 0.0
    for p in sorted(parts, key=lambda p: -p["bytes_per_s"]):
        if cur and (cur_rate + p["bytes_per_s"] > WORKER_BYTES_PER_S or len(cur) >= WORKER_MAX_TENANTS):
            workers.append(cur)
            cur, cur_rate = [], 0.0
        cur.append(p)
        cur_rate += p["bytes_per_s"]
    if cur:
        workers.append(cur)
    return workers


def estimate(gbph, minutes, n_tenants, n_workers):
    """Rough platform cost of a step (list prices), before running it."""
    gb = gbph * minutes / 60
    json_gb = gb * 2.5          # OTLP JSON (what Firehose carries) is ~2.5x the protobuf
    requests = gb * 1e9 / 150_000
    raw_files = n_tenants * 3 * minutes * 2      # every stream flushes every 30 s when busy (upper bound)
    ingest_gbs = requests * 0.4 * 1.0             # ~0.4 s at 1 GB
    indexer_gbs = raw_files * 1.5 * 2.0           # ~1.5 s at 2 GB
    loadgen_gbs = (n_workers + 1) * 60 * minutes * 1.77      # senders + prober, busy or not
    c = (requests * (PRICE["apigw_request"] + 2 * PRICE["lambda_request"])
         + (ingest_gbs + indexer_gbs + loadgen_gbs) * PRICE["lambda_gb_s_arm"]
         + json_gb * PRICE["firehose_gb"] + raw_files * (PRICE["s3_put"] + PRICE["eventbridge_event"]))
    return c


def run(args):
    keys = _load_keys(args.keys)
    workers = plan_workers(keys, args.gbph)
    est = estimate(args.gbph, args.minutes, len(keys), len(workers))
    print(f"step {args.step}: {args.gbph} GB/h for {args.minutes} min across {len(keys)} tenants, "
          f"{len(workers)} loadgen invocations per window; estimated platform cost ~${est:.2f} "
          "(+ compaction, measured afterwards)")
    if not args.yes:
        print("re-run with --yes to start")
        return
    ep = endpoint()
    probes = [{"tenant": tenant_name(r), "key": keys[tenant_name(r)]["key"]} for r in PROBE_RANKS
              if tenant_name(r) in keys]
    start = time.time()
    end = start + args.minutes * 60
    runs = _load_runs()
    runs[args.step] = {"gbph": args.gbph, "minutes": args.minutes, "tenants": len(keys),
                       "workers": len(workers), "start": int(start), "end": int(end)}
    _save_runs(runs)
    window = 0
    while time.time() < end - 30:
        dur = min(WINDOW_S, end - time.time())
        for i, ts in enumerate(workers):
            invoke("obs-loadgen", {"mode": "send", "run": RUN, "step": args.step, "worker": f"{window}-{i}",
                                   "endpoint": ep, "duration_s": dur, "tenants": ts}, asynchronous=True)
        if dur > 360:
            invoke("obs-loadgen", {"mode": "probe", "run": RUN, "step": args.step, "endpoint": ep,
                                   "duration_s": dur, "interval_s": 60, "tenants": probes}, asynchronous=True)
        print(f"{datetime.now(timezone.utc):%H:%M:%S} window {window}: {len(workers)} senders for {dur:.0f}s",
              flush=True)
        window += 1
        time.sleep(dur)
    print("load finished; results land in obs-loadtest within a few minutes. Then: report --step", args.step)


def _items(step):
    items, kw = [], dict(TableName="obs-loadtest", KeyConditionExpression="pk = :p",
                         ExpressionAttributeValues={":p": {"S": f"{RUN}#{step}"}})
    while True:
        page = ddb.query(**kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _n(it, k, default=0.0):
    return float(it[k]["N"]) if k in it else default


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def metric(ns, name, dims, stat, start, end, period=60):
    """Aggregate of one CloudWatch metric over [start, end]: sum of Sums, max of Maximums, avg of Averages."""
    q = [{"Id": "m", "MetricStat": {"Metric": {"Namespace": ns, "MetricName": name,
                                               "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
                                    "Period": period, "Stat": stat}}]
    vals = cw.get_metric_data(MetricDataQueries=q, StartTime=start, EndTime=end)["MetricDataResults"][0]["Values"]
    if not vals:
        return 0.0
    if stat == "Sum" or stat == "SampleCount":
        return sum(vals)
    if stat == "Average":
        return statistics.mean(vals)
    return max(vals)


def lambda_stats(fn, start, end):
    d = {"FunctionName": fn}
    inv = metric("AWS/Lambda", "Invocations", d, "Sum", start, end)
    dur = metric("AWS/Lambda", "Duration", d, "Average", start, end)
    return {"invocations": inv, "errors": metric("AWS/Lambda", "Errors", d, "Sum", start, end),
            "throttles": metric("AWS/Lambda", "Throttles", d, "Sum", start, end),
            "avg_ms": dur, "p99_ms": metric("AWS/Lambda", "Duration", d, "p99", start, end),
            "max_concurrency": metric("AWS/Lambda", "ConcurrentExecutions", d, "Maximum", start, end),
            "gb_s": inv * dur / 1000 * (MEMORY_MB.get(fn) or 1024) / 1024}


def firehose_totals(tenants, start, end):
    queries = []
    for i, t in enumerate(sorted(tenants)):
        for sig in ("logs", "traces", "metrics"):
            d = [{"Name": "DeliveryStreamName", "Value": f"obs-t-{t}-{sig}"}]
            for m, stat in (("IncomingBytes", "Sum"), ("IncomingRecords", "Sum"),
                            ("DeliveryToS3.DataFreshness", "Maximum"), ("ThrottledRecords", "Sum")):
                queries.append({"Id": f"q{len(queries)}", "Label": m, "MetricStat": {
                    "Metric": {"Namespace": "AWS/Firehose", "MetricName": m, "Dimensions": d},
                    "Period": 300, "Stat": stat}})
    tot = {"IncomingBytes": 0.0, "IncomingRecords": 0.0, "DeliveryToS3.DataFreshness": 0.0, "ThrottledRecords": 0.0}
    for i in range(0, len(queries), 500):
        kw = dict(MetricDataQueries=queries[i:i + 500], StartTime=start, EndTime=end)
        while True:
            resp = cw.get_metric_data(**kw)
            for r in resp["MetricDataResults"]:
                if r["Label"] == "DeliveryToS3.DataFreshness":
                    tot[r["Label"]] = max([tot[r["Label"]], *r["Values"]])
                else:
                    tot[r["Label"]] += sum(r["Values"])
            if "NextToken" not in resp:
                break
            kw["NextToken"] = resp["NextToken"]
    return tot


def report(args):
    items = _items(args.step)
    run_ = _load_runs().get(args.step)
    if not run_:
        sys.exit(f"no run {args.step} in {RUNS_FILE}")
    meta = {k: {"N": str(v)} for k, v in run_.items()}
    start = datetime.fromtimestamp(_n(meta, "start"), timezone.utc)
    end = datetime.fromtimestamp(_n(meta, "end"), timezone.utc) + timedelta(minutes=2)
    minutes = (end - start).total_seconds() / 60
    sends = [i for i in items if i["sk"]["S"].startswith("send#")]
    probes = [i for i in items if i["sk"]["S"].startswith("probe#")]
    keys = _load_keys(args.keys, missing_ok=True)

    print(f"== step {args.step}: target {_n(meta, 'gbph'):g} GB/h, {minutes:.0f} min, "
          f"{_n(meta, 'tenants'):.0f} tenants, {_n(meta, 'workers'):.0f} senders/window "
          f"({start:%Y-%m-%d %H:%M}Z)")
    sent = sum(_n(i, "bytes") for i in sends)
    status = {}
    for i in sends:
        for k, v in json.loads(i["status"]["S"]).items():
            status[k] = status.get(k, 0) + v
    reqs = sum(_n(i, "requests") for i in sends)
    print(f"sent: {sent / 1e9:.2f} GB protobuf = {sent / 1e9 / (minutes / 60):.2f} GB/h achieved, "
          f"{reqs:.0f} requests ({reqs / minutes / 60:.1f}/s); gzip on the wire "
          f"{sum(_n(i, 'gz_bytes') for i in sends) / 1e9:.2f} GB")
    print(f"  HTTP statuses {status}; retries {sum(_n(i, 'retries') for i in sends):.0f}; "
          f"dropped after retries {sum(_n(i, 'dropped') for i in sends):.0f}")
    print(f"  request latency p50 ~{pct([_n(i, 'lat_p50') for i in sends], 0.5):.2f}s, "
          f"p99 (worst sender) {max([_n(i, 'lat_p99') for i in sends] or [0]):.2f}s; "
          f"generator max behind schedule {max([_n(i, 'max_behind_s') for i in sends] or [0]):.1f}s")

    fresh = [_n(p, "freshness_s") for p in probes if _n(p, "freshness_s") >= 0]
    lost = [p for p in probes if _n(p, "freshness_s") < 0]
    print(f"freshness (send -> findable by trace id): {len(fresh)} probes, p50 {pct(fresh, .5):.0f}s, "
          f"p90 {pct(fresh, .9):.0f}s, p99 {pct(fresh, .99):.0f}s, max {max(fresh or [0]):.0f}s; "
          f"not found within 300s: {len(lost)}  (target 60s)")
    for label in ("1h", "24h", "24h_id"):
        s = [_n(p, f"lookup_{label}_s") for p in probes if f"lookup_{label}_s" in p]
        c = [_n(p, f"lookup_{label}_candidates") for p in probes if f"lookup_{label}_s" in p]
        r = [_n(p, f"lookup_{label}_rcu") for p in probes if f"lookup_{label}_s" in p]
        if s:
            print(f"lookup {label:6}: p50 {pct(s, .5) * 1000:.0f} ms, p99 {pct(s, .99) * 1000:.0f} ms, "
                  f"candidates max {max(c):.0f}, read units max {max(r):.1f}")

    print("platform:")
    fns = {fn: lambda_stats(fn, start, end) for fn in ("obs-ingest", "obs-ingest-authorizer", "obs-recent-indexer",
                                                        "obs-index-lookup")}
    for fn, s in fns.items():
        print(f"  {fn:22} {s['invocations']:8.0f} inv, {s['errors']:.0f} errors, {s['throttles']:.0f} throttles, "
              f"avg {s['avg_ms']:.0f} ms, p99 {s['p99_ms']:.0f} ms, max concurrency {s['max_concurrency']:.0f}")
    api = {"ApiName": "obs-ingest", "Stage": "ingest"}
    print(f"  API Gateway: {metric('AWS/ApiGateway', 'Count', api, 'Sum', start, end):.0f} requests, "
          f"{metric('AWS/ApiGateway', '4XXError', api, 'Sum', start, end):.0f} 4xx, "
          f"{metric('AWS/ApiGateway', '5XXError', api, 'Sum', start, end):.0f} 5xx, "
          f"latency p99 {metric('AWS/ApiGateway', 'Latency', api, 'p99', start, end):.0f} ms")
    fh = firehose_totals(keys or [tenant_name(r) for r in range(int(_n(meta, 'tenants')))], start, end)
    print(f"  Firehose: {fh['IncomingBytes'] / 1e9:.2f} GB JSON in {fh['IncomingRecords']:.0f} records, "
          f"{fh['ThrottledRecords']:.0f} throttled, max delivery freshness {fh['DeliveryToS3.DataFreshness']:.0f}s")
    wru = metric("AWS/DynamoDB", "ConsumedWriteCapacityUnits", {"TableName": "obs-index"}, "Sum", start, end)
    rru = metric("AWS/DynamoDB", "ConsumedReadCapacityUnits", {"TableName": "obs-index"}, "Sum", start, end)
    print(f"  obs-index: {wru:.0f} write units, {rru:.0f} read units")

    raw_files = fns["obs-recent-indexer"]["invocations"]
    billed_gb = max(fh["IncomingBytes"], fh["IncomingRecords"] * 5120) / 1e9
    cost = {
        "API Gateway": fns["obs-ingest"]["invocations"] * PRICE["apigw_request"],
        "ingest + authorizer Lambda": sum((fns[f]["gb_s"] * PRICE["lambda_gb_s_arm"] + fns[f]["invocations"]
                                           * PRICE["lambda_request"]) for f in ("obs-ingest", "obs-ingest-authorizer")),
        "Firehose": billed_gb * PRICE["firehose_gb"],
        "fast lane (indexer Lambda)": fns["obs-recent-indexer"]["gb_s"] * PRICE["lambda_gb_s_arm"]
                                      + raw_files * PRICE["lambda_request"],
        "S3 PUTs + S3 events": raw_files * (PRICE["s3_put"] + PRICE["eventbridge_event"]),
        "index writes": wru * PRICE["ddb_wru"],
    }
    total = sum(cost.values())
    gb = sent / 1e9
    print(f"cost of ingest + fast lane (list prices; compaction and storage separate): ${total:.3f} "
          f"= ${total / gb if gb else 0:.3f}/GB")
    for k, v in sorted(cost.items(), key=lambda kv: -kv[1]):
        print(f"  {k:28} ${v:.3f}  ({100 * v / total if total else 0:.0f}%)")


def compaction(args):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.since_minutes)
    w = lambda_stats("obs-compaction-worker", start, end)
    d = lambda_stats("obs-compaction-dispatcher", start, end)
    print(f"compaction over the last {args.since_minutes} min:")
    print(f"  worker: {w['invocations']:.0f} chunks, {w['errors']:.0f} errors, avg {w['avg_ms'] / 1000:.1f}s, "
          f"p99 {w['p99_ms'] / 1000:.1f}s, max concurrency {w['max_concurrency']:.0f}, "
          f"${w['gb_s'] * PRICE['lambda_gb_s_arm']:.3f}")
    print(f"  dispatcher: {d['invocations']:.0f} runs, {d['errors']:.0f} errors, avg {d['avg_ms'] / 1000:.1f}s, "
          f"max {metric('AWS/Lambda', 'Duration', {'FunctionName': 'obs-compaction-dispatcher'}, 'Maximum', start, end) / 1000:.1f}s (timeout 120s)")
    for sig in ("logs", "traces", "metrics"):
        age = metric("obs", "OldestIncomingAgeMinutes", {"signal": sig}, "Maximum", start, end, period=900)
        print(f"  oldest uncompacted {sig}: max {age:.0f} min (alarm at 180)")


# ------------------------------------------------------------------ keys

def _load_keys(path, missing_ok=False):
    if not os.path.exists(path):
        if missing_ok:
            return {}
        sys.exit(f"no keys file {path}; run 'tenants create' first")
    with open(path) as f:
        return json.load(f)


def _load_runs():
    if not os.path.exists(RUNS_FILE):
        return {}
    with open(RUNS_FILE) as f:
        return json.load(f)


def _save_runs(runs):
    with open(RUNS_FILE, "w") as f:
        json.dump(runs, f, indent=1)


def _save_keys(path, keys):
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(keys, f)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keys", default=KEYS_DEFAULT, help="tenant keys file (keep it out of the repo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    t = sub.add_parser("tenants")
    t.add_argument("what", choices=["create", "delete"])
    t.add_argument("--n", type=int, default=100)
    r = sub.add_parser("run")
    r.add_argument("--gbph", type=float, required=True)
    r.add_argument("--minutes", type=float, default=45)
    r.add_argument("--step", required=True)
    r.add_argument("--yes", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("--step", required=True)
    c = sub.add_parser("compaction")
    c.add_argument("--since-minutes", type=int, default=180)
    a = p.parse_args()
    if a.cmd == "tenants":
        (tenants_create if a.what == "create" else tenants_delete)(a)
    else:
        {"preflight": preflight, "run": run, "report": report, "compaction": compaction}[a.cmd](a)


if __name__ == "__main__":
    main()
