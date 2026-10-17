"""The Kubernetes and GPU pages' queries over what real collectors sent: 21 minutes captured from the
GPU lab (infra/gpu-lab.sh: k3s 1.36 on a g4dn.12xlarge, 4x Tesla T4, the collectors installed by
examples/kubernetes/leasyd-k8s.sh --gpu, infra/gpu-lab/workloads.yaml) in testdata/k8s-gpu-capture.json.gz.
Where test_infra_queries.py checks the queries against the demo's model of the collectors, this
checks them against the collectors themselves (label names, metric shapes, k3s quirks).

What ran: trainer (Deployment, team research) 2 GPUs busy (nbody, ~90%); embeddings-batch (Job, team
research) 2 GPUs held but idle until it completed at 00:46:57; jupyter-lab (StatefulSet, team
data-science) pending for want of a GPU until then, then holding one idle; email crash-looping; cart
fine. Needs node (the web app's node_modules) to read the queries; skipped without it."""
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from test_infra_queries import pytestmark, queries  # noqa: F401  (same skip rule)

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(HERE, "testdata", "k8s-gpu-capture.json.gz")


def _times(docs):
    for doc in docs:
        for r in doc.get("resourceMetrics", []) + doc.get("resourceLogs", []):
            for s in r.get("scopeMetrics", []) + r.get("scopeLogs", []):
                for m in s.get("metrics", []):
                    for kind in ("gauge", "sum"):
                        for p in (m.get(kind) or {}).get("dataPoints", []):
                            yield int(p["timeUnixNano"])
                for rec in s.get("logRecords", []):
                    t = int(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano") or 0)
                    if t:
                        yield t


@pytest.fixture(scope="module")
def lab():
    from localengine import LocalEngine
    data = json.load(gzip.open(CAPTURE, "rt"))
    e, lo, hi = LocalEngine(), None, None
    for f in data["files"]:
        ts = list(_times(f["docs"]))
        first, last = (datetime.fromtimestamp(t / 1e9, timezone.utc) for t in (min(ts), max(ts)))
        e.load(f["signal"], f["docs"], first)
        lo, hi = min(lo or first, first), max(hi or last, last)
    qs = queries()

    def run(name):
        return e.records(qs[name]["sql"], lo - timedelta(minutes=1), hi + timedelta(minutes=1))
    yield run
    e.close()


def test_discovery_and_cluster(lab):
    assert {(r["kind"], r["service"]) for r in lab("discovery")} == {
        ("gpu", "dcgm-exporter"), ("k8s", "kubernetes"), ("events", "kubernetes")}
    assert {r["cluster"] for r in lab("clusters")} == {"gpu-lab"}
    assert {r["namespace"] for r in lab("clusters")} >= {"ml", "notebooks", "shop", "leasyd", "kube-system"}


def test_gpu_fleet_from_real_dcgm_labels(lab):
    rows = lab("gpuFleet")
    assert len(rows) == 4 and {r["model"] for r in rows} == {"Tesla T4"}
    assert {(r["node"].startswith("ip-"), r["cluster"], r["instance_type"], r["cloud"]) for r in rows} == {
        (True, "gpu-lab", "g4dn.12xlarge", "aws")}
    busy = [r for r in rows if r["active_hours"] > 0]
    assert len(busy) == 2 and all(r["util_avg"] > 80 and r["idle_hours"] == 0 for r in busy)
    idle = [r for r in rows if r["active_hours"] == 0]
    assert len(idle) == 2 and all(r["util_avg"] == 0 and r["idle_hours"] > 0 for r in idle)
    assert all(abs(r["hours"] - (r["active_hours"] + r["idle_hours"] + r["free_hours"])) < 1e-9 for r in rows)
    assert all(14000 < r["mem_total"] < 16000 for r in rows)   # T4: 15 GB


def test_gpu_states_over_time(lab):
    rows = lab("gpuSeries")
    assert rows and all(r["gpus"] == 4 and r["active"] + r["idle"] + r["free"] == 4 for r in rows)
    assert all(r["active"] == 2 for r in rows)
    assert rows[0]["idle"] == 2 and rows[0]["free"] == 0        # the batch job holds 2 idle
    assert rows[-1]["idle"] == 1 and rows[-1]["free"] == 1      # it finished; the notebook took one


def test_gpu_time_per_workload_and_team(lab):
    by = {r["workload"]: r for r in lab("gpuWorkloads")}
    assert set(by) == {"trainer", "embeddings-batch", "jupyter-lab"}
    t, b, j = by["trainer"], by["embeddings-batch"], by["jupyter-lab"]
    assert (t["kind"], t["team"], t["gpus"], t["pods"], t["holding_now"]) == ("Deployment", "research", 2, 2, 2)
    assert t["active_hours"] == t["hours"] and t["util_avg"] > 80
    assert (b["kind"], b["team"], b["gpus"], b["active_hours"], b["holding_now"]) == ("Job", "research", 2, 0, 0)
    assert b["idle_hours"] == b["hours"] > 0
    assert (j["kind"], j["team"], j["gpus"], j["active_hours"], j["holding_now"]) == ("StatefulSet", "data-science", 1, 0, 1)
    assert lab("gpuDays") == [{"day": "2026-10-17", "gpus_max": 4, "held_max": 4, "active_max": 2,
                               "active_p95": 2.0, "active_avg": 2.0}]


def test_pod_waiting_for_a_gpu(lab):
    rows = lab("gpuStalled")
    assert [(r["namespace"], r["pod"]) for r in rows] == [("notebooks", "jupyter-lab-0")]
    assert "Insufficient nvidia.com/gpu" in rows[0]["message"]


def test_nodes_from_kubeletstats_and_k8s_cluster(lab):
    [n] = lab("k8sNodes")
    assert (n["cluster"], n["instance_type"], n["ready"], n["alloc_cpu"], n["alloc_pods"]) == (
        "gpu-lab", "g4dn.12xlarge", 1.0, 48.0, 110.0)
    assert 1.5 < n["cpu"] < 3 and n["mem"] > 1e9                 # 2 busy trainers ~1 core each
    assert 0 < n["fs_used"] < n["fs_cap"]


def test_pods_workloads_and_events(lab):
    pods = {r["pod"]: r for r in lab("k8sPods")}
    trainers = [r for r in pods.values() if r["workload"] == "trainer"]
    assert len(trainers) == 2 and all(r["kind"] == "Deployment" and r["team"] == "research" and r["cpu"] > 0.5
                                      for r in trainers)
    [email] = [r for r in pods.values() if r["workload"] == "email"]
    assert email["restarts_total"] >= 5 and email["team"] == "shop"
    assert {r["workload"] for r in pods.values() if r["namespace"] == "leasyd"} == {
        "leasyd-agent-opentelemetry-collector-agent", "leasyd-cluster-opentelemetry-collector", "leasyd-dcgm-dcgm-exporter"}
    wl = {(r["namespace"], r["name"]): r for r in lab("k8sWorkloads")}
    assert (wl[("ml", "trainer")]["desired"], wl[("ml", "trainer")]["ready"]) == (2.0, 2.0)
    assert wl[("notebooks", "jupyter-lab")]["kind"] == "StatefulSet"
    ev = {(r["object"].split("-")[0], r["reason"]): r for r in lab("k8sEvents")}
    assert ev[("email", "BackOff")]["warning"] and ev[("email", "BackOff")]["events"] >= 10
    assert ev[("jupyter", "FailedScheduling")]["warning"]
    assert ("embeddings", "Completed") in ev


def test_usage_by_namespace(lab):
    rows = lab("k8sUsageNs")
    ml = [r["cpu"] for r in rows if r["key"] == "ml"]
    assert ml and min(ml) > 1.5                                  # the two trainers
    assert {r["key"] for r in rows} >= {"ml", "shop", "leasyd", "kube-system"}
