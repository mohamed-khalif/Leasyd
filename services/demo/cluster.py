"""The demo shop's Kubernetes cluster and its GPUs, as Leasyd's Kubernetes collectors send them
(examples/kubernetes: kubeletstats, k8s_cluster, k8s_events, and dcgm-exporter scraped by the
agent). Same attribute names, units and metric types: the contract in docs/gpu-kubernetes.md.

Cluster "shop-prod" (EKS, us-east-1), one minute at a time, deterministic per minute:
  - 3 CPU nodes running the shop (the same pods demo.py's traces and logs come from) and the
    usual system pods; a GPU node pool: two g5.12xlarge (4 x NVIDIA A10G each) and one
    p4d.24xlarge (8 x NVIDIA A100 40GB): 16 GPUs
  - ml-training: llm-finetune (2 workers x 4 A100, 85-97 % busy, between runs 02:00-04:00 UTC the
    GPUs sit unallocated); embeddings-batch waits for 4 GPUs (Pending, FailedScheduling events)
  - ml-inference: reco-inference (3 pods x 1 A10G, busy with the shop's traffic; one GPU runs hot
    at the afternoon peak and is throttled), embed-service (1 A10G)
  - notebooks: jupyter-alice and jupyter-bob each hold an A10G and mostly leave it idle; two A10G
    are unallocated
  - trouble: once a day at 13:05 UTC an A100 reports XID 48 (double-bit ECC error) and a worker
    restarts; during the shop's incident (12 min every 3 h) the email pod crash-loops; the
    recommendation pod is OOM-killed every 6 hours
"""

import hashlib
import math
import random

CLUSTER = "shop-prod"
REGION = "us-east-1"
DRIVER = "550.127.08"
KUBELET_SCOPE = "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/kubeletstatsreceiver"
CLUSTER_SCOPE = "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/k8sclusterreceiver"
EVENTS_SCOPE = "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/k8seventsreceiver"
PROM_SCOPE = "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/prometheusreceiver"
GIB = 2**30

# node -> (instance type, zone, vCPU, memory GiB, GPU model, GPUs)
NODES = {
    "ip-10-0-1-37.ec2.internal": ("m6i.2xlarge", "us-east-1a", 8, 32, None, 0),
    "ip-10-0-2-114.ec2.internal": ("m6i.2xlarge", "us-east-1b", 8, 32, None, 0),
    "ip-10-0-3-9.ec2.internal": ("m6i.2xlarge", "us-east-1c", 8, 32, None, 0),
    "ip-10-0-4-21.ec2.internal": ("g5.12xlarge", "us-east-1a", 48, 192, "NVIDIA A10G", 4),
    "ip-10-0-4-58.ec2.internal": ("g5.12xlarge", "us-east-1b", 48, 192, "NVIDIA A10G", 4),
    "ip-10-0-5-140.ec2.internal": ("p4d.24xlarge", "us-east-1a", 96, 1152, "NVIDIA A100-SXM4-40GB", 8),
}
CPU_NODES = [n for n, v in NODES.items() if not v[5]]
GPU_MEMORY_MIB = {"NVIDIA A10G": 23028, "NVIDIA A100-SXM4-40GB": 40960}
TRAINING_GAP = (120, 240)          # minutes of the UTC day between fine-tuning runs
XID_AT = 13 * 60 + 5               # minute of the UTC day of the daily XID 48


def _h(s, n=10):
    return hashlib.md5(s.encode()).hexdigest()[:n]


_K8S = "bcdfghjklmnpqrstvwxz2456789"   # the characters Kubernetes uses in generated name suffixes


def _k(s, n):
    d = hashlib.md5(s.encode()).digest()
    return "".join(_K8S[b % len(_K8S)] for b in d[:n])


def _uuid(node, i):
    h = hashlib.md5(f"{node}/{i}".encode()).hexdigest()
    return f"GPU-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class Pod:
    """A pod: where it runs, what owns it, what it asks for and (for GPU pods) which GPUs."""

    def __init__(self, ns, name, kind, owner, node, cpu_req, mem_req_gib, cpu_lim=None, mem_lim_gib=None,
                 team=None, app=None, gpus=(), container=None):
        self.ns, self.name, self.kind, self.owner, self.node = ns, name, kind, owner, node
        self.cpu_req, self.mem_req, self.cpu_lim = cpu_req, mem_req_gib * GIB, cpu_lim
        self.mem_lim = (mem_lim_gib * GIB) if mem_lim_gib else None
        self.team, self.app, self.gpus = team, app or owner, list(gpus)
        self.container = container or (app or owner)
        self.uid = "%s-%s-%s-%s-%s" % tuple(_h(f"uid/{ns}/{name}", 32)[i:j] for i, j in ((0, 8), (8, 12), (12, 16), (16, 20), (20, 32)))


def _replica(owner, i, kind="Deployment"):
    if kind == "Deployment":
        return f"{owner}-{_k('rs/' + owner, 9 if len(owner) % 2 else 10)}-{_k(f'{owner}/{i}', 5)}"
    if kind in ("DaemonSet", "Job"):
        return f"{owner}-{_k(f'{owner}/{i}', 5)}"
    return f"{owner}-{i}"


def _pods():
    import demo   # the shop's pods, as its traces and logs name them
    pods = []
    for svc, names in demo.PODS.items():
        for name in names:
            node = demo.NODES[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(demo.NODES)]
            mem = {"java": 1.0, "dotnet": 0.6, "nodejs": 0.4, "python": 0.4}.get(demo.SERVICES[svc][0], 0.25)
            pods.append(Pod("shop", name, "Deployment", svc, node, 0.2, mem, cpu_lim=1.0, mem_lim_gib=mem * 2, team="shop", app=svc))
    for i, node in enumerate(NODES):
        pods.append(Pod("kube-system", _replica("aws-node", i, "DaemonSet"), "DaemonSet", "aws-node", node, 0.025, 0.05))
        pods.append(Pod("kube-system", _replica("kube-proxy", i, "DaemonSet"), "DaemonSet", "kube-proxy", node, 0.1, 0.05))
        pods.append(Pod("leasyd", _replica("leasyd-agent", i, "DaemonSet"), "DaemonSet", "leasyd-agent", node, 0.1, 0.25,
                        mem_lim_gib=1, app="opentelemetry-collector"))
    for i in range(2):
        pods.append(Pod("kube-system", _replica("coredns", i), "Deployment", "coredns", CPU_NODES[i], 0.1, 0.07, mem_lim_gib=0.17))
    pods.append(Pod("leasyd", _replica("leasyd-cluster", 0), "Deployment", "leasyd-cluster", CPU_NODES[2], 0.05, 0.125,
                    mem_lim_gib=0.5, app="opentelemetry-collector"))
    gpu_nodes = [n for n, v in NODES.items() if v[5]]
    a, b, p = gpu_nodes
    for i, node in enumerate(gpu_nodes):
        pods.append(Pod("gpu-operator", _replica("nvidia-dcgm-exporter", i, "DaemonSet"), "DaemonSet", "nvidia-dcgm-exporter", node, 0.1, 0.5,
                        app="nvidia-dcgm-exporter"))
    for w in range(2):
        pods.append(Pod("ml-training", f"llm-finetune-worker-{w}", "Job", "llm-finetune", p, 40, 480, team="research",
                        app="llm-finetune", gpus=[(p, 4 * w + g) for g in range(4)], container="trainer"))
    pods.append(Pod("ml-training", _replica("embeddings-batch", 0, "Job"), "Job", "embeddings-batch", None, 16, 64, team="research",
                    app="embeddings-batch", container="embed"))
    for i in range(3):
        pods.append(Pod("ml-inference", _replica("reco-inference", i), "Deployment", "reco-inference", a, 4, 24, cpu_lim=8,
                        mem_lim_gib=32, team="search", app="reco-inference", gpus=[(a, i)], container="server"))
    pods.append(Pod("ml-inference", _replica("embed-service", 0), "Deployment", "embed-service", b, 4, 16, cpu_lim=8, mem_lim_gib=24,
                    team="search", app="embed-service", gpus=[(b, 2)], container="server"))
    for i, who in enumerate(("alice", "bob")):
        pods.append(Pod("notebooks", f"jupyter-{who}", "StatefulSet", f"jupyter-{who}", b, 2, 16, cpu_lim=4, mem_lim_gib=32,
                        team="data-science", app="jupyter", gpus=[(b, i)], container="notebook"))
    return pods


PODS = None


def pods():
    global PODS
    if PODS is None:
        PODS = _pods()
    return PODS


def _incident(minute):
    import demo
    return demo.incident(minute)


def _load(minute):
    import demo
    return demo.load(minute)


def training(minute):
    return not (TRAINING_GAP[0] <= minute % 1440 < TRAINING_GAP[1])


def running(pod, minute):
    """Is the pod running this minute (else Pending, or for finished training: gone)?"""
    if pod.owner == "embeddings-batch":
        return False
    if pod.owner == "llm-finetune":
        return training(minute)
    return True


# ------------------------------------------------------------------ OTLP helpers

def _kv(d):
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": k, "value": {"doubleValue": v}})
        else:
            out.append({"key": k, "value": {"stringValue": str(v)}})
    return out


def _point(t, v, attrs=None):
    p = {"timeUnixNano": t}
    if isinstance(v, int):
        p["asInt"] = str(v)
    else:
        p["asDouble"] = round(float(v), 6)
    if attrs:
        p["attributes"] = _kv(attrs)
    return p


def _gauge(name, unit, pts):
    return {"name": name, "unit": unit, "gauge": {"dataPoints": pts}}


def _sum(name, unit, pts, monotonic=True, start=None):
    for p in pts:
        p["startTimeUnixNano"] = start or p["timeUnixNano"]
    return {"name": name, "unit": unit, "sum": {"aggregationTemporality": 2, "isMonotonic": monotonic, "dataPoints": pts}}


def _node_res(node, extra=None):
    itype, zone = NODES[node][0], NODES[node][1]
    return {"k8s.cluster.name": CLUSTER, "k8s.node.name": node, "cloud.provider": "aws", "cloud.platform": "aws_eks",
            "cloud.region": REGION, "cloud.availability_zone": zone, "host.type": itype, "host.name": node,
            **(extra or {})}


def _pod_res(pod):
    owner_key = {"Deployment": "k8s.deployment.name", "StatefulSet": "k8s.statefulset.name", "DaemonSet": "k8s.daemonset.name",
                 "Job": "k8s.job.name"}[pod.kind]
    res = {"k8s.cluster.name": CLUSTER, "k8s.namespace.name": pod.ns, "k8s.pod.name": pod.name, "k8s.pod.uid": pod.uid,
           owner_key: pod.owner, "k8s.pod.labels.team": pod.team, "k8s.pod.labels.app": pod.app}
    if pod.kind == "Deployment":
        res["k8s.replicaset.name"] = pod.name.rsplit("-", 1)[0]
    if pod.node:
        res.update(_node_res(pod.node))
    return res


def _resource(attrs, scope, metrics=None, logs=None, service="kubernetes"):
    res = {"resource": {"attributes": _kv({"service.name": service, **attrs})}}
    if metrics is not None:
        res["scopeMetrics"] = [{"scope": {"name": scope, "version": "0.162.0"}, "metrics": metrics}]
    else:
        res["scopeLogs"] = [{"scope": {"name": scope, "version": "0.162.0"}, "logRecords": logs}]
    return res


# ------------------------------------------------------------------ GPUs

class Gpu:
    def __init__(self, node, index):
        self.node, self.index = node, index
        self.model = NODES[node][4]
        self.uuid = _uuid(node, index)
        self.pci = f"00000000:{0x10 + index * 0x10:02X}:00.0"


def gpus():
    return [Gpu(n, i) for n, v in NODES.items() for i in range(v[5])]


def holder(gpu, minute):
    for p in pods():
        if (gpu.node, gpu.index) in p.gpus and running(p, minute):
            return p
    return None


def gpu_state(gpu, minute, r):
    """(utilization %, memory used MiB, temperature C, power W, thermal throttling ns this minute)."""
    p = holder(gpu, minute)
    load, hour = _load(minute), (minute % 1440) / 60
    mem_total = GPU_MEMORY_MIB[gpu.model]
    if p is None:
        util, mem = 0.0, 0
    elif p.owner == "llm-finetune":
        util, mem = r.uniform(85, 97), int(mem_total * r.uniform(0.86, 0.93))
    elif p.owner == "reco-inference":
        util, mem = max(5.0, 22 + 60 * load + r.gauss(0, 4)), int(mem_total * 0.61)
    elif p.owner == "embed-service":
        util, mem = max(3.0, 30 + 20 * load + r.gauss(0, 5)), int(mem_total * 0.42)
    else:   # notebooks: a model loaded, the GPU mostly waiting
        busy = p.name == "jupyter-alice" and 14 <= hour < 15
        util, mem = (r.uniform(55, 75) if busy else r.uniform(0, 2)), int(mem_total * (0.55 if p.name == "jupyter-alice" else 0.31))
    tdp = {"NVIDIA A10G": 300, "NVIDIA A100-SXM4-40GB": 400}[gpu.model]
    throttled_ns = throttled(gpu, minute)
    temp = 33 + 0.5 * util + r.gauss(0, 1) + (14 if throttled_ns else 0)
    power = tdp * (0.2 + 0.75 * util / 100) + r.gauss(0, 3)
    return util, mem, temp, power, throttled_ns


def hot(gpu):
    return gpu.node == "ip-10-0-4-21.ec2.internal" and gpu.index == 1


def throttled(gpu, minute):
    """Nanoseconds the hot GPU spent thermally throttled this minute (afternoon peak only)."""
    if not hot(gpu) or _load(minute) <= 0.85:
        return 0
    return int(random.Random(f"thr/{minute}").uniform(18, 35) * 1e9)


def xid_gpu(gpu):
    return gpu.node == "ip-10-0-5-140.ec2.internal" and gpu.index == 5


def xids_this_week(minute):
    """The daily XID 48s so far this week (the ECC counters count since the driver loaded: weekly)."""
    return (minute % 10080) // 1440 + (1 if minute % 1440 >= XID_AT else 0)


def gpu_metrics(minute):
    """One resource (the dcgm-exporter target) per GPU node; its metrics as the prometheus receiver
    turns dcgm-exporter's output into OTLP (gauges; counters as cumulative sums)."""
    t = str((minute * 60 + 59) * 10**9)
    day0 = minute - minute % 1440
    out = []
    by_node = {}
    for g in gpus():
        by_node.setdefault(g.node, []).append(g)
    for node, gs in by_node.items():
        series = {}
        for g in gs:
            r = random.Random(f"gpu/{g.uuid}/{minute}")
            util, mem, temp, power, _ = gpu_state(g, minute, r)
            p = holder(g, minute)
            labels = {"gpu": str(g.index), "UUID": g.uuid, "pci_bus_id": g.pci, "device": f"nvidia{g.index}",
                      "modelName": g.model, "hostname": node, "DCGM_FI_DRIVER_VERSION": DRIVER, "node": node}
            if p:
                labels.update({"namespace": p.ns, "pod": p.name, "container": p.container, "team": p.team,
                               "app_kubernetes_io_name": p.app})
            xid = 48 if xid_gpu(g) and minute % 1440 == XID_AT else 0
            dbe = xids_this_week(minute) if xid_gpu(g) else 0
            vals = {
                ("DCGM_FI_DEV_GPU_UTIL", "gauge"): float(round(util)),
                ("DCGM_FI_PROF_GR_ENGINE_ACTIVE", "gauge"): util / 100 * 0.97,
                ("DCGM_FI_PROF_SM_ACTIVE", "gauge"): util / 100 * 0.91,
                ("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "gauge"): util / 100 * (0.62 if g.model.startswith("NVIDIA A100") else 0.35),
                ("DCGM_FI_PROF_DRAM_ACTIVE", "gauge"): util / 100 * 0.48,
                ("DCGM_FI_DEV_FB_USED", "gauge"): float(mem),
                ("DCGM_FI_DEV_FB_FREE", "gauge"): float(GPU_MEMORY_MIB[g.model] - mem),
                ("DCGM_FI_DEV_GPU_TEMP", "gauge"): float(round(temp)),
                ("DCGM_FI_DEV_MEMORY_TEMP", "gauge"): float(round(temp + 6)),
                ("DCGM_FI_DEV_POWER_USAGE", "gauge"): round(power, 3),
                ("DCGM_FI_DEV_SM_CLOCK", "gauge"): float(1410 if util > 5 else 210),
                ("DCGM_FI_DEV_MEM_CLOCK", "gauge"): float(1215 if g.model.startswith("NVIDIA A100") else 6250),
                ("DCGM_FI_DEV_XID_ERRORS", "gauge"): float(xid),
                ("DCGM_FI_DEV_ROW_REMAP_FAILURE", "gauge"): 0.0,
                ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "counter"): float(dbe),
                ("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "counter"): float(dbe * 3),
                ("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", "counter"): float(dbe),
                ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "counter"): 0.0,
                ("DCGM_FI_DEV_THERMAL_VIOLATION", "counter"): float(sum(throttled(g, m) for m in range(day0, minute + 1)) if hot(g) else 0),
                ("DCGM_FI_DEV_POWER_VIOLATION", "counter"): 0.0,
            }
            for (name, kind), v in vals.items():
                series.setdefault((name, kind), []).append(_point(t, float(v), labels))
        metrics = []
        for (name, kind), pts in series.items():
            if kind == "gauge":
                metrics.append(_gauge(name, "", pts))
            else:
                metrics.append(_sum(name, "", pts, start=str(day0 * 60 * 10**9)))
        out.append(_resource({**_node_res(node), "service.instance.id": f"{node}:9400", "server.address": node,
                              "server.port": "9400", "url.scheme": "http"}, PROM_SCOPE, metrics=metrics, service="dcgm-exporter"))
    return out


# ------------------------------------------------------------------ Kubernetes

def _restarts(pod, minute):
    """Container restarts since the pod started (cumulative), and the waiting reason now."""
    if pod.owner == "email":
        incidents = sum(1 for m in range(minute - minute % 180, minute + 1) if _incident(m) and m % 3 == 0)
        before = ((minute % 10080) // 180) * 4
        reason = "CrashLoopBackOff" if _incident(minute) else None
        return before + incidents, reason
    if pod.owner == "recommendation":
        return (minute % 10080) // 360, ("OOMKilled" if minute % 360 == 0 else None)   # the pod is replaced weekly
    if pod.owner == "llm-finetune" and pod.name.endswith("-1"):
        return xids_this_week(minute), None
    return 0, None


def kube_metrics(minute):
    t = str((minute * 60 + 59) * 10**9)
    r = random.Random(f"k8s/{minute}")
    load, out = _load(minute), []
    node_cpu, node_mem, node_pods = {}, {}, {}
    for p in pods():
        if not running(p, minute):
            continue
        if p.ns == "shop":
            cpu = 0.03 + 0.25 * load * r.uniform(0.7, 1.3) * (3 if p.owner == "shipping" and _incident(minute) else 1)
            mem = p.mem_req * (0.7 + 0.35 * ((minute % 17) / 17)) * (1.85 if p.owner == "recommendation" else 1)
        elif p.gpus:
            cpu = p.cpu_req * r.uniform(0.3, 0.6)
            mem = p.mem_req * r.uniform(0.6, 0.9)
        else:
            cpu, mem = p.cpu_req * r.uniform(0.2, 0.8), p.mem_req * r.uniform(0.5, 0.9)
        node_cpu[p.node] = node_cpu.get(p.node, 0) + cpu
        node_mem[p.node] = node_mem.get(p.node, 0) + mem
        node_pods[p.node] = node_pods.get(p.node, 0) + 1
        ms = [_gauge("k8s.pod.cpu.usage", "{cpu}", [_point(t, round(cpu, 5))]),
              _gauge("k8s.pod.memory.working_set", "By", [_point(t, int(mem))]),
              _gauge("k8s.pod.cpu_request_utilization", "1", [_point(t, round(cpu / p.cpu_req, 4))]),
              _gauge("k8s.pod.memory_request_utilization", "1", [_point(t, round(mem / p.mem_req, 4))])]
        if p.cpu_lim:
            ms.append(_gauge("k8s.pod.cpu_limit_utilization", "1", [_point(t, round(cpu / p.cpu_lim, 4))]))
        if p.mem_lim:
            ms.append(_gauge("k8s.pod.memory_limit_utilization", "1", [_point(t, round(mem / p.mem_lim, 4))]))
        out.append(_resource(_pod_res(p), KUBELET_SCOPE, metrics=ms))
    for node, (itype, zone, vcpu, mem_gib, model, n_gpu) in NODES.items():
        base_cpu = 0.25 + node_cpu.get(node, 0)
        base_mem = 1.4 * GIB + node_mem.get(node, 0) + (9 * GIB if node == "ip-10-0-3-9.ec2.internal" else 0)
        fs_cap = 100 * GIB if not n_gpu else 500 * GIB
        out.append(_resource(_node_res(node), KUBELET_SCOPE, metrics=[
            _gauge("k8s.node.cpu.usage", "{cpu}", [_point(t, round(base_cpu, 4))]),
            _gauge("k8s.node.memory.working_set", "By", [_point(t, int(base_mem))]),
            _gauge("k8s.node.memory.available", "By", [_point(t, int(mem_gib * GIB - base_mem))]),
            _gauge("k8s.node.filesystem.usage", "By", [_point(t, int(fs_cap * (0.38 if not n_gpu else 0.61)))]),
            _gauge("k8s.node.filesystem.capacity", "By", [_point(t, int(fs_cap))]),
        ]))
        out.append(_resource({"k8s.cluster.name": CLUSTER, "k8s.node.name": node, "k8s.node.uid": _h("node/" + node, 32)}, CLUSTER_SCOPE, metrics=[
            _gauge("k8s.node.condition_ready", "", [_point(t, 1)]),
            _gauge("k8s.node.condition_memory_pressure", "", [_point(t, 0)]),
            _gauge("k8s.node.allocatable_cpu", "{cpu}", [_point(t, float(vcpu) - 0.08)]),
            _gauge("k8s.node.allocatable_memory", "By", [_point(t, float(mem_gib * GIB * 0.93))]),
            _gauge("k8s.node.allocatable_pods", "{pod}", [_point(t, 110 if not n_gpu else 234)]),
        ]))
    # the Kubernetes API's view of every pod and container (the cluster collector)
    for p in pods():
        if p.owner == "llm-finetune" and not training(minute):
            continue      # between runs the job's pods are gone
        phase = 2 if running(p, minute) else 1
        res = {"k8s.cluster.name": CLUSTER, "k8s.namespace.name": p.ns, "k8s.pod.name": p.name, "k8s.pod.uid": p.uid,
               **({"k8s.node.name": p.node} if phase == 2 else {})}
        out.append(_resource(res, CLUSTER_SCOPE, metrics=[_gauge("k8s.pod.phase", "", [_point(t, phase)])]))
        restarts, reason = _restarts(p, minute)
        if phase == 1:
            reason = None
        cres = {**res, "k8s.container.name": p.container, "container.image.name": f"registry.example.com/{p.app}",
                "container.image.tag": "1.4.2"}
        ms = [_gauge("k8s.container.restarts", "{restart}", [_point(t, restarts)]),
              _gauge("k8s.container.ready", "", [_point(t, 0 if (reason or phase == 1) else 1)]),
              _gauge("k8s.container.cpu_request", "{cpu}", [_point(t, float(p.cpu_req))]),
              _gauge("k8s.container.memory_request", "By", [_point(t, int(p.mem_req))])]
        if p.cpu_lim:
            ms.append(_gauge("k8s.container.cpu_limit", "{cpu}", [_point(t, float(p.cpu_lim))]))
        if p.mem_lim:
            ms.append(_gauge("k8s.container.memory_limit", "By", [_point(t, int(p.mem_lim))]))
        if reason:
            ms.append(_sum("k8s.container.status.reason", "{container}",
                           [_point(t, 1, {"k8s.container.status.reason": reason})], monotonic=False))
        out.append(_resource(cres, CLUSTER_SCOPE, metrics=ms))
    # workloads
    owners = {}
    for p in pods():
        owners.setdefault((p.ns, p.kind, p.owner), []).append(p)
    for (ns, kind, owner), ps in sorted(owners.items()):
        up = sum(1 for p in ps if running(p, minute) and not _restarts(p, minute)[1])
        res = {"k8s.cluster.name": CLUSTER, "k8s.namespace.name": ns}
        if kind == "Deployment":
            res["k8s.deployment.name"] = owner
            ms = [_gauge("k8s.deployment.desired", "{pod}", [_point(t, len(ps))]), _gauge("k8s.deployment.available", "{pod}", [_point(t, up)])]
        elif kind == "StatefulSet":
            res["k8s.statefulset.name"] = owner
            ms = [_gauge("k8s.statefulset.desired_pods", "{pod}", [_point(t, len(ps))]), _gauge("k8s.statefulset.ready_pods", "{pod}", [_point(t, up)])]
        elif kind == "DaemonSet":
            res["k8s.daemonset.name"] = owner
            ms = [_gauge("k8s.daemonset.desired_scheduled_nodes", "{node}", [_point(t, len(ps))]),
                  _gauge("k8s.daemonset.ready_nodes", "{node}", [_point(t, up)])]
        else:
            if owner == "llm-finetune" and not training(minute):
                continue
            res["k8s.job.name"] = owner
            ms = [_gauge("k8s.job.active_pods", "{pod}", [_point(t, sum(1 for p in ps if running(p, minute) or owner == "embeddings-batch"))]),
                  _gauge("k8s.job.failed_pods", "{pod}", [_point(t, 0)]), _gauge("k8s.job.successful_pods", "{pod}", [_point(t, 0)])]
        out.append(_resource(res, CLUSTER_SCOPE, metrics=ms))
    return out


def kube_events(minute):
    """Kubernetes events (k8s_events receiver): message in the body, reason as an attribute."""
    t = (minute * 60 + 30) * 10**9
    recs = []

    def ev(kind, name, ns, reason, message, warning=True, node=None, count=1):
        res = {"k8s.cluster.name": CLUSTER, "k8s.object.kind": kind, "k8s.object.name": name,
               "k8s.object.uid": _h(f"obj/{ns}/{name}", 32), "k8s.object.api_version": "v1", "k8s.node.name": node or ""}
        rec = {"timeUnixNano": str(t), "severityNumber": 13 if warning else 9, "severityText": "Warning" if warning else "Normal",
               "body": {"stringValue": message},
               "attributes": _kv({"k8s.event.reason": reason, "k8s.event.action": "", "k8s.namespace.name": ns,
                                  "k8s.event.name": f"{name}.{_h(f'{name}/{reason}/{minute}', 16)}", "k8s.event.count": count})}
        recs.append(_resource(res, EVENTS_SCOPE, logs=[rec]))

    batch = next(p for p in pods() if p.owner == "embeddings-batch")
    if minute % 5 == 0:
        ev("Pod", batch.name, batch.ns, "FailedScheduling",
           "0/6 nodes are available: 3 Insufficient nvidia.com/gpu, 3 node(s) didn't match Pod's node affinity/selector. "
           "preemption: 0/6 nodes are available: 3 No preemption victims found for incoming pod, 3 Preemption is not helpful for scheduling.",
           count=1 + (minute % 1440) // 5)
    email = next(p for p in pods() if p.owner == "email")
    if _incident(minute) and minute % 3 == 0:
        ev("Pod", email.name, "shop", "BackOff", f"Back-off restarting failed container {email.container} in pod {email.name}_shop({email.uid})",
           node=email.node)
    rec = next(p for p in pods() if p.owner == "recommendation")
    if minute % 360 == 0:
        ev("Pod", rec.name, "shop", "OOMKilling", f"Memory cgroup out of memory: Killed process 2731 (python) total-vm:2048000kB, "
           f"anon-rss:{int(rec.mem_lim / 1024)}kB", node=rec.node)
    if minute % 1440 == XID_AT:
        worker = next(p for p in pods() if p.name == "llm-finetune-worker-1")
        ev("Pod", worker.name, worker.ns, "BackOff", f"Back-off restarting failed container trainer in pod {worker.name}_{worker.ns}({worker.uid})",
           node=worker.node)
    if minute % 1440 == TRAINING_GAP[1]:
        for w in range(2):
            ev("Pod", f"llm-finetune-worker-{w}", "ml-training", "Scheduled",
               f"Successfully assigned ml-training/llm-finetune-worker-{w} to ip-10-0-5-140.ec2.internal", warning=False,
               node="ip-10-0-5-140.ec2.internal")
    return recs


def documents(minute):
    """{"metrics": [resourceMetrics], "logs": [resourceLogs]}: this minute of the cluster and its GPUs."""
    return {"metrics": kube_metrics(minute) + gpu_metrics(minute), "logs": kube_events(minute)}
