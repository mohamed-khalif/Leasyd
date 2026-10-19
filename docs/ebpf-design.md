# Zero-code APM with eBPF: every service on the map in a minute

Status: **built 2026-10-19 (E1–E4, `docs/refactor-progress.md`); verified on a real node; pending deploy.** Builds on the Kubernetes install (`examples/kubernetes`), the host
install (`examples/ec2/setup.sh`), the service map and OpenTelemetry ingest.

## What a customer gets

One command they already run (`leasyd-k8s.sh` for a cluster, `setup.sh` for a server, or the ECS-on-EC2
template) now also instruments **every service on those machines, in any language, with no code,
image or deployment change**:

- **Service map:** every service and what it calls (other services, databases, caches, queues,
  external APIs), with requests a minute, error rate and latency on each node and each connection.
- **Service health:** requests, errors and duration (p50/p95/p99) per service and per route
  (`GET /api/orders/{id}`), HTTP, gRPC, SQL, Redis, Kafka and more.
- **Traces:** each request's spans (incoming request, its outgoing calls and queries), kept for every
  error and every slow request plus a sample of the rest.
- **The bigger picture:** the same service name ties the eBPF data to the cluster (Kubernetes page:
  workload, pod, node), the pod logs already collected, alerts and SLOs. A service later
  instrumented with an OpenTelemetry SDK just gets deeper; nothing is counted twice.

Where it can't run (no access to the host): AWS Fargate, Lambda, Cloud Run. Those keep the
in-app OpenTelemetry agent; the map shows both.

## How it works

The agent is **OpenTelemetry eBPF Instrumentation (OBI)**, the OpenTelemetry project's eBPF
instrumenter (formerly Grafana Beyla), Apache-2.0, pinned to `v0.14.0` (overridable). We package
and configure it; we don't write instrumentation.

```
 node / server
 ┌───────────────────────────────────────────────┐
 │ apps (any language) ──▶ kernel ◀── OBI (eBPF) │
 │                                     │ OTLP     │
 │                                     ▼ :14318   │
 │      Leasyd collector (already installed)      │──▶ Leasyd
 │      eBPF pipelines: tail sampling, cluster    │
 └───────────────────────────────────────────────┘
```

- **Kubernetes:** OBI is a DaemonSet (`examples/kubernetes/ebpf.yaml`: privileged, `hostPID`,
  read-only RBAC on pods, replicasets, services, nodes). It names services after their workload
  (Deployment, StatefulSet...) and adds namespace, pod and node. It sends to its node's Leasyd
  collector on a port of its own (`14318`, host port), so the eBPF data has its own pipelines.
- **Servers (EC2, VMs):** `setup.sh` installs the OBI binary (checksum-verified) as a systemd
  service next to the collector, sending to `localhost:14318`.
- **ECS on EC2:** a daemon service template (`examples/aws/ecs-ebpf.yaml`): the collector and OBI
  on every container instance. OBI names each container after its ECS service / task family
  (needs `ecs:ListTasks`, `ecs:DescribeTasks`, given to the template's task role).
- **One collector egress, one key:** OBI never holds the API key; the collector does.

## Efficiency decisions

1. **The map and health come from metrics, not spans.** OBI aggregates in the kernel/agent and
   sends, per minute, small histograms and counters:
   - `http.server.request.duration`, `http.client.request.duration`, `rpc.*`, `db.client.operation.duration`,
     messaging durations (feature `application_red`): requests, errors (`error.type`), latency
     per service and route;
   - `traces_service_graph_request_total` / `_failed_total` and `traces_service_graph_request_server`
     / `_client` (feature `application_service_graph`): one series per connection
     (`client` → `server`, `connection_type`).

   A busy service is a few dozen series a minute whatever its traffic. Network-flow and
   TCP-stat features are off (high volume, not needed for the map).
2. **The service map reads those metrics with one aggregate query** (counter rise per connection
   and per server), not the span self-join it runs for SDK traces. Both feed the map; where both
   see the same connection, the larger count wins (never summed).
3. **Traces are tail-sampled on each node** before they leave it: every trace with an error, every
   span slower than 1 s, and 10% of the rest (by trace id, so a sampled trace is kept on every
   node). `--ebpf-traces 0` sends none, `100` all. Metrics are computed before sampling, so they
   stay exact.
4. **No double counting with SDKs:** OBI stops its own traces / metrics for a process it sees
   exporting OTLP itself (`exclude_otel_instrumented_services`, on by default).
5. **Cardinality:** routes are grouped by OBI's heuristic (`/orders/123` → `/orders/*`), at most
   10 values per path segment per service; client IPs are not labels.

Rough volume, 20 services at 50 requests/s each: metrics ~3 MB/day; traces at 10% plus errors and
slow ~1–2 GB/day (vs ~15 GB/day unsampled).

## The edges, exactly

`traces_service_graph_request_total` counts a call once: on the server side when the server is
instrumented, on the client side (`connection_type` `virtual_node` / `database` / ...) when it isn't.
`traces_service_graph_request_failed_total` counts on both sides, so per connection the app takes
the server-side series when there are any, else the client-side ones. Peers OBI can only name by
IP address collapse into one "outside" node.

## Build plan

| Step | What |
|---|---|
| E1 | Install: `ebpf.yaml` + collector pipelines + `leasyd-k8s.sh --ebpf-traces` / `--no-ebpf`; `setup.sh` OBI; ECS-on-EC2 template |
| E2 | Lab on a real node (k3s, uninstrumented services in 3 languages, a database): capture OBI's real OTLP into `testdata/` |
| E3 | App: service map and service panel from the eBPF metrics (one query), merged with SDK edges; tests on the capture |
| E4 | Demo data, Get started ("Kubernetes" / "Linux server" say "every service, no code", a new "Amazon ECS on EC2"), docs |

## Not in this step

- Fargate / Lambda (no host): an init-container injector for the in-app agents.
- Network flows between zones, TCP retransmits (OBI has them; volume first).
- Payload capture (SQL text, GenAI tokens through eBPF).
- Alert templates on the eBPF metrics (a connection's error rate, a route's p95): the span-based ones already cover services with SDK traces.
