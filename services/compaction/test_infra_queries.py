"""The Kubernetes and GPU pages' queries (services/web/src/infra/queries.ts), run on the real query
engine (fast lane, DuckDB, the SQL lock-down) over 2.5 hours of the demo cluster
(services/demo/cluster.py), which sends what Leasyd's collectors send (docs/gpu-kubernetes.md).
Each answer is checked against what the demo is known to hold. Needs node (the web app's
node_modules) to read the queries; skipped without it."""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "web")
sys.path.insert(0, os.path.join(HERE, "..", "demo"))

DAY0 = int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp()) // 60
START, END = DAY0 + 12 * 60, DAY0 + 14 * 60 + 30          # 12:00-14:30 UTC: an incident, the XID at 13:05, the peak
T0, T1 = (datetime.fromtimestamp(m * 60, timezone.utc) for m in (START, END))
pytestmark = pytest.mark.skipif(not (shutil.which("node") and os.path.isdir(os.path.join(WEB, "node_modules", "esbuild"))),
                                reason="needs node and services/web/node_modules")


def queries(extra=()):
    out = subprocess.run(["node", "scripts/export-infra-queries.mjs", json.dumps(list(extra))], cwd=WEB, check=True,
                         capture_output=True, text=True)
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def eng():
    import cluster
    from localengine import LocalEngine, load_minutes
    e = LocalEngine()

    def docs(m):
        d = cluster.documents(m)
        return {"metrics": {"resourceMetrics": d["metrics"]}, "logs": {"resourceLogs": d["logs"]}}
    load_minutes(e, docs, range(START, END))
    yield e
    e.close()


@pytest.fixture(scope="module")
def hint(eng):
    rows = eng.records(queries()["discovery"]["sql"], T0, T1)
    by = {}
    for r in rows:
        by.setdefault(r["kind"], set()).add(r["service"])
    return by


def run(eng, hint, name, extra=()):
    qs = queries(extra)
    q = qs[name]
    services = sorted(set().union(*(hint.get(u, set()) for u in q["uses"]))) if q["uses"] else None
    rows = eng.records(q["sql"], T0, T1, services=services or None)
    if services:   # the hint only skips files: the answer is the same without it (sums may differ in the last digits)
        assert _norm(rows) == _norm(eng.records(q["sql"], T0, T1)), f"{name}: the services hint changed the answer"
    return rows


def _norm(rows):
    return [{k: float(f"{v:.9g}") if isinstance(v, float) else v for k, v in r.items()} for r in rows]


def test_discovery_finds_the_collectors_services(hint):
    assert hint == {"gpu": {"dcgm-exporter"}, "k8s": {"kubernetes"}, "events": {"kubernetes"}}


def test_gpu_fleet(eng, hint):
    rows = run(eng, hint, "gpuFleet")
    assert len(rows) == 16
    assert sorted({(r["model"], sum(1 for x in rows if x["model"] == r["model"])) for r in rows}) == [
        ("NVIDIA A100-SXM4-40GB", 8), ("NVIDIA A10G", 8)]
    by_node = {}
    for r in rows:
        by_node.setdefault(r["node"], []).append(r)
    assert {n: len(v) for n, v in by_node.items()} == {"ip-10-0-4-21.ec2.internal": 4, "ip-10-0-4-58.ec2.internal": 4,
                                                       "ip-10-0-5-140.ec2.internal": 8}
    hours = (END - START) / 60
    for r in rows:
        assert r["hours"] == pytest.approx(hours, abs=0.05)
        assert r["active_hours"] + r["idle_hours"] + r["free_hours"] == pytest.approx(r["hours"])
        assert r["cluster"] == "shop-prod" and r["cloud"] == "aws" and r["mem_total"] > 20000
    held = {r["holder_now"] for r in rows}
    assert {"ml-training/llm-finetune-worker-0", "ml-training/llm-finetune-worker-1", "notebooks/jupyter-bob"} <= held
    free = [r for r in rows if r["holder_now"] == ""]
    assert len(free) == 2 and all(r["free_hours"] == pytest.approx(hours, abs=0.05) for r in free)
    bob = next(r for r in rows if r["holder_now"] == "notebooks/jupyter-bob")
    assert bob["idle_hours"] == pytest.approx(hours, abs=0.05) and bob["team_now"] == "data-science"
    a100 = [r for r in rows if r["model"].startswith("NVIDIA A100")]
    assert all(r["util_avg"] > 80 for r in a100)
    assert len(run(eng, hint, "gpuFleetHour")) == 16


def test_gpu_series_splits_every_gpu(eng, hint):
    rows = run(eng, hint, "gpuSeries")
    assert len(rows) >= 25
    for r in rows:
        assert r["gpus"] == 16 and r["active"] + r["idle"] + r["free"] == 16 and 0 < r["mem"] < 1


def test_gpu_workloads_attribute_time_to_owners(eng, hint):
    rows = {r["workload"]: r for r in run(eng, hint, "gpuWorkloads")}
    assert set(rows) == {"llm-finetune", "reco-inference", "embed-service", "jupyter-alice", "jupyter-bob"}
    ft = rows["llm-finetune"]
    assert (ft["namespace"], ft["kind"], ft["team"], ft["gpus"], ft["pods"]) == ("ml-training", "Job", "research", 8, 2)
    assert ft["idle_hours"] == 0 and ft["holding_now"] == 8
    reco = rows["reco-inference"]
    assert (reco["kind"], reco["team"], reco["gpus"], reco["pods"]) == ("Deployment", "search", 3, 3)
    bob = rows["jupyter-bob"]
    assert bob["kind"] == "StatefulSet" and bob["active_hours"] == 0 and bob["idle_hours"] > 2


def test_gpu_health_finds_the_xid_and_the_hot_gpu(eng, hint):
    rows = run(eng, hint, "gpuHealth")
    xid = [r for r in rows if r["xids"]]
    assert len(xid) == 1 and xid[0]["xids"] == [48] and xid[0]["dbe"] == 1 and xid[0]["uncorrectable_rows"] == 1
    assert xid[0]["xid_last"].startswith("2026-10-14T13:05") and xid[0]["holder"] == "ml-training/llm-finetune-worker-1"
    hot = [r for r in rows if (r["thermal_s"] or 0) > 0]
    assert len(hot) == 1 and hot[0]["node"] == "ip-10-0-4-21.ec2.internal" and hot[0]["temp_max"] >= 85
    events = run(eng, hint, "gpuXids")
    assert [(e["xid"], e["first"][:16], e["last"][:16]) for e in events] == [(48, "2026-10-14T13:05", "2026-10-14T13:05")]


def test_stalled_gpu_workloads(eng, hint):
    rows = run(eng, hint, "gpuStalled")
    assert len(rows) == 1
    r = rows[0]
    assert r["namespace"] == "ml-training" and r["pod"].startswith("embeddings-batch-") and r["phase"] == 1
    assert r["events"] == 30 and "Insufficient nvidia.com/gpu" in r["message"]


def test_gpu_days(eng, hint):
    rows = run(eng, hint, "gpuDays")
    assert len(rows) == 1 and rows[0]["gpus_max"] == 16 and rows[0]["held_max"] == 14 and 8 <= rows[0]["active_max"] <= 13


def test_gpu_detail(eng, hint):
    gpu = run(eng, hint, "gpuFleet")[0]["gpu"]
    rows = run(eng, hint, "x", [["x", "gpuDetail", gpu, 300]])
    assert len(rows) == 30 and all(r["util"] is not None and r["mem_used"] > 0 for r in rows)


def test_k8s_nodes(eng, hint):
    rows = {r["node"]: r for r in run(eng, hint, "k8sNodes")}
    assert len(rows) == 6
    for r in rows.values():
        assert r["ready"] == 1 and r["cpu"] > 0 and r["alloc_cpu"] > 7 and r["alloc_mem"] > 0 and r["pods"] >= 3
        assert r["cluster"] == "shop-prod" and r["instance_type"] and r["zone"]
    assert rows["ip-10-0-5-140.ec2.internal"]["gpus"] == 8 and rows["ip-10-0-1-37.ec2.internal"]["gpus"] == 0
    assert rows["ip-10-0-5-140.ec2.internal"]["instance_type"] == "p4d.24xlarge"


def test_k8s_pods(eng, hint):
    rows = run(eng, hint, "k8sPods")
    by = {r["pod"]: r for r in rows}
    import cluster
    assert len(rows) == len(cluster.pods())
    batch = next(r for p, r in by.items() if p.startswith("embeddings-batch"))
    assert batch["phase"] == 1 and batch["cpu"] is None and batch["node"] == ""
    email = next(r for p, r in by.items() if p.startswith("email-"))
    assert email["phase"] == 2 and email["restarts"] == 3 and email["workload"] == "email" and email["kind"] == "Deployment"
    assert email["reason"] == ""     # the crash-loop was over by 14:30
    worker = by["llm-finetune-worker-1"]
    assert worker["restarts"] == 1 and worker["team"] == "research" and worker["kind"] == "Job"
    assert all(r["cluster"] == "shop-prod" for r in rows)
    shop = run(eng, hint, "k8sPodsNs")
    assert {r["namespace"] for r in shop} == {"shop"} and len(shop) == 24


def test_k8s_workloads_and_events(eng, hint):
    rows = {(r["namespace"], r["name"]): r for r in run(eng, hint, "k8sWorkloads")}
    assert rows[("shop", "checkout")]["kind"] == "Deployment" and rows[("shop", "checkout")]["desired"] == 2
    assert rows[("notebooks", "jupyter-bob")]["kind"] == "StatefulSet"
    assert rows[("kube-system", "aws-node")]["desired"] == 6
    events = run(eng, hint, "k8sEvents")
    reasons = {r["reason"] for r in events}
    assert {"FailedScheduling", "BackOff", "OOMKilling"} <= reasons
    assert events[0]["warning"] is True


def test_k8s_usage_and_pod_detail(eng, hint):
    ns = run(eng, hint, "k8sUsageNs")
    assert {r["key"] for r in ns} >= {"shop", "ml-training", "ml-inference", "notebooks", "kube-system", "leasyd"}
    nodes = run(eng, hint, "k8sUsageNode")
    assert len({r["key"] for r in nodes}) == 6
    import cluster
    pod = next(p for p in cluster.pods() if p.owner == "recommendation")
    series = run(eng, hint, "s", [["s", "k8sPodSeries", "shop", pod.name, 300]])
    assert len(series) == 30 and all(r["mem"] > 0 and r["mem_limit"] > 0 for r in series)
    cont = run(eng, hint, "c", [["c", "k8sContainers", "shop", pod.name]])
    assert len(cont) == 1 and cont[0]["mem_limit"] == int(pod.mem_lim) and cont[0]["restarts"] == 0
    oom = run(eng, hint, "e", [["e", "k8sEvents", {}, {"namespace": "shop", "name": pod.name}]])
    assert [e["reason"] for e in oom] == ["OOMKilling"]


def test_builtin_dashboards_query(eng):
    """Every PromQL of the built-in Kubernetes and NVIDIA GPU dashboards runs and finds data
    ($cluster at "any", as the dashboard sends it)."""
    import promql
    import query
    t0, t1, step = int(T0.timestamp()), int(T1.timestamp()), 300

    def run_query(q):
        return query.run(q, invoke_worker=query.run_worker)
    for name, p in queries()["dashboards"].items():
        filled = p["promql"].replace("$__interval", f"{step}s").replace("$__range", f"{t1 - t0}s").replace("$cluster", ".*")
        if p["type"] == "stat":   # number panels: one evaluation over the range, at its end
            out = promql.query_instant("acme", filled, t1, run_query=run_query)
        else:
            out = promql.query_range("acme", filled, t0, t1, step, run_query=run_query)
        assert out["status"] == "success" and out["data"]["result"], f"{name}: no data for {filled}"


def test_alert_templates_find_the_demo_trouble(eng):
    """The Kubernetes and GPU alert templates run as the alerts service runs them (one PromQL
    evaluation), and fire on what the demo is known to hold."""
    import promql
    import query

    def run_query(q):
        return query.run(q, invoke_worker=query.run_worker)

    def firing(t, at):   # at the degraded level when there is one, else critical
        out = promql.query_instant("acme", t["promql"], int(at.timestamp()), run_query=run_query)
        vals = [float(r["value"][1]) for r in out["data"]["result"]]
        level = t.get("degraded", t["critical"])
        return [v for v in vals if (v > level if t["op"] == ">" else v < level)]
    at = lambda h, m: datetime.fromtimestamp((DAY0 + h * 60 + m) * 60, timezone.utc)   # noqa: E731
    infra = [t for t in queries()["alertTemplates"] if t["group"] in ("Kubernetes", "GPUs")]   # AI agents: test_agents.py
    templates = {t["name"]: t for t in infra}
    expect = {"Pod crash-looping": at(12, 11),   # 3 restarts seen (the data starts at 12:00): degraded "Pod stuck pending": at(14, 0), "GPU XID error": at(13, 7),
              "GPU double-bit ECC errors": at(13, 8), "GPU thermal throttling": at(13, 30), "GPU held but idle": at(14, 0)}
    for name, when in expect.items():
        assert firing(templates[name], when), f"{name} should fire at {when}"
    quiet = {"Node not ready": at(14, 0), "Node memory high": at(14, 0), "Deployment missing replicas": at(13, 30),
             "GPU XID error": at(14, 20), "Pod crash-looping": at(14, 20)}
    for name, when in quiet.items():
        assert not firing(templates[name], when), f"{name} should be quiet at {when}"
    assert set(templates) == {t["name"] for t in infra} and len(templates) == 11
    for t in templates.values():   # the rest at least evaluate
        firing(t, at(14, 0))
