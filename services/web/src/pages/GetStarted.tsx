// Get started: from an empty account to data in Leasyd, in three numbered steps that tick off as they're done.
// 1. an API key (create one, shown once, or paste one you have); 2. what to monitor, grouped (your apps, infrastructure,
// websites, cloud integrations): copy-paste setup with the key and endpoint filled in; 3. a live check: what Leasyd has
// received today (the account's meter) and which services are searchable yet (a count by service over the last
// 15 minutes, until some show up).
// Install files (collector.sh, the Ruby runtime metrics, the AWS templates) are served by the web app
// itself under /install/ (infra/deploy-phaseW1.sh copies them from examples/).
import { ReactNode, useEffect, useMemo, useState } from "react";
import { fmtNum } from "../lib/format";
import { account, query, records, rumSnippet } from "../api";
import type { Ctx } from "../App";
import { useAccount } from "../useAccount";
import { getConfig } from "../config";
import { IconCheck, IconGlobe, IconGpu, IconK8s, IconKey, IconLambda, IconLogs, IconServer } from "../icons";

type Source = "server" | "kubernetes" | "gpu" | "java" | "nodejs" | "python" | "ruby" | "other" | "lambda" | "aws-logs" | "web";
type Signal = "Traces" | "Logs" | "Metrics" | "Events";
type Group = "apps" | "infra" | "web" | "cloud";
interface SourceInfo { id: Source; name: string; desc: string; group: Group; icon: ReactNode; signals: Signal[] }

// Languages get a short monogram rather than a logo.
const mono = (text: string) => <span className="start-mono">{text}</span>;
const SOURCES: SourceInfo[] = [
  { id: "java", name: "Java", desc: "Java agent, no code changes", group: "apps", icon: mono("Jv"), signals: ["Traces", "Logs", "Metrics"] },
  { id: "nodejs", name: "Node.js", desc: "No code changes needed", group: "apps", icon: mono("JS"), signals: ["Traces", "Metrics"] },
  { id: "python", name: "Python", desc: "No code changes needed", group: "apps", icon: mono("Py"), signals: ["Traces", "Logs", "Metrics"] },
  { id: "ruby", name: "Ruby", desc: "SDK plus runtime metrics", group: "apps", icon: mono("Rb"), signals: ["Traces", "Metrics"] },
  { id: "other", name: "Other languages", desc: ".NET, Go, PHP: any OpenTelemetry SDK", group: "apps", icon: mono("</>"), signals: ["Traces", "Logs", "Metrics"] },
  { id: "server", name: "Linux server", desc: "Host metrics and system logs", group: "infra", icon: <IconServer />, signals: ["Logs", "Metrics"] },
  { id: "kubernetes", name: "Kubernetes", desc: "The whole cluster, one Helm install", group: "infra", icon: <IconK8s />, signals: ["Logs", "Metrics", "Events"] },
  { id: "gpu", name: "NVIDIA GPUs", desc: "Usage, health and cost", group: "infra", icon: <IconGpu />, signals: ["Metrics"] },
  { id: "web", name: "Website (real users)", desc: "Real visitors' speed and errors", group: "web", icon: <IconGlobe />, signals: ["Events"] },
  { id: "lambda", name: "AWS Lambda", desc: "Metrics for every function", group: "cloud", icon: <IconLambda />, signals: ["Metrics", "Traces"] },
  { id: "aws-logs", name: "AWS logs (CloudWatch)", desc: "Logs already in CloudWatch", group: "cloud", icon: <IconLogs />, signals: ["Logs"] },
];
const GROUPS: [Group, string, string][] = [
  ["apps", "Your apps", "Any language, through OpenTelemetry"],
  ["infra", "Infrastructure", "On any cloud or your own hardware"],
  ["web", "Websites", "What real visitors experience"],
  ["cloud", "Cloud integrations", "Data your cloud provider already collects"],
];
const SIGNALS = ["traces", "logs", "metrics"] as const;
const POLL_MS = 10_000;

export function GetStarted({ ctx, initial }: { ctx: Ctx; initial?: string | null }) {
  const { account: acc, error: accError } = useAccount(0, POLL_MS);   // what Leasyd has received today, every POLL_MS
  const [actionError, setError] = useState<string | null>(null);
  const error = actionError ?? accError;
  const [key, setKey] = useState("");
  const [created, setCreated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<Source>(SOURCES.find((x) => x.id === initial)?.id ?? "server");
  const [baseline, setBaseline] = useState<number | null>(null);
  const [services, setServices] = useState<Record<string, string[]>>({});

  const endpoint = (getConfig().ingestUrl || "https://ingest.leasyd.com").replace(/\/$/, "");
  const site = location.origin;
  const k = key.trim() || "<your API key>";

  if (baseline === null && acc) setBaseline(acc.today?.records ?? 0);   // the count when the page opened

  // Which services are searchable: the last 15 minutes, by signal, until some show up.
  const found = SIGNALS.some((s) => services[s]?.length);
  useEffect(() => {
    if (found) return;
    let live = true;
    const look = () => {
      const end = new Date(), start = new Date(end.getTime() - 15 * 60_000);
      for (const signal of SIGNALS) {
        query({ signal, start: start.toISOString(), end: end.toISOString(), group_by: ["service"], aggs: [{ fn: "count" }], limit: 20 })
          .then((r) => live && setServices((s) => ({ ...s, [signal]: records(r).map((x) => String(x.service)).filter(Boolean) })), () => undefined);
      }
    };
    look();
    const t = setInterval(look, 2 * POLL_MS);
    return () => { live = false; clearInterval(t); };
  }, [found]);

  const createKey = async () => {
    setBusy(true); setError(null);
    try {
      const r = await account.createKey("ingest");
      setKey(r.api_key); setCreated(true);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };

  const owner = acc?.you.role === "owner";
  const today = acc?.today?.records ?? 0;
  const arrived = baseline != null && today > baseline;
  const received = arrived || today > 0;
  const allServices = useMemo(() => [...new Set(SIGNALS.flatMap((s) => services[s] ?? []))].sort(), [services]);
  const chosen = SOURCES.find((x) => x.id === source)!;
  const done = [key.trim() !== "", received, found];

  return (
    <div className="start">
      <header className="start-hero">
        <div>
          <h2>Connect your first data source</h2>
          <p>Logs, traces and metrics from any cloud, server or app. Leasyd speaks OpenTelemetry, the open standard,
            so it works wherever your code runs and nothing locks you in.</p>
        </div>
        <ol className="start-progress" aria-label="Progress">
          {["API key", "Connect a source", "See your data"].map((label, i) => (
            <li key={label} className={done[i] ? "done" : ""}>
              <span className="start-progress-dot" aria-hidden>{done[i] ? <IconCheck /> : i + 1}</span>{label}
            </li>
          ))}
        </ol>
      </header>
      {error && <div className="form-error" role="alert">{error}</div>}

      <Step n={1} done={done[0]} title="Get an API key" hint="Your apps send data with a key that can only send, never read.">
        {created ? (
          <div className="start-newkey">
            <b>Your new key: copy it now, it won't be shown again.</b>
            <span className="faint">It starts working within about 2 minutes, and it's already filled in below.</span>
            <div className="newkey-row"><code className="mono">{key}</code><CopyButton text={key} /></div>
          </div>
        ) : (
          <div className="start-keyrow">
            {owner
              ? <button className="btn primary" onClick={createKey} disabled={busy}><IconKey />{busy ? "Creating…" : "Create a key"}</button>
              : acc && <span className="faint">Only an owner of the account can create keys; ask one, or paste a key you have.</span>}
            <span className="faint">or paste one you have</span>
            <input className="input mono grow" placeholder="obs_…" value={key} onChange={(e) => setKey(e.target.value)}
                   aria-label="Your API key" autoComplete="off" spellCheck={false} />
          </div>
        )}
      </Step>

      <Step n={2} done={done[1]} title="Choose what to monitor" hint="Pick one to start; you can add the others any time.">
        <div className="start-groups">
          {GROUPS.map(([group, label, sub]) => (
            <section key={group} className={`start-group grp-${group}`}>
              <h4>{label} <span className="faint">{sub}</span></h4>
              <div className="start-sources" role="tablist" aria-label={label}>
                {SOURCES.filter((x) => x.group === group).map((x) => (
                  <button key={x.id} role="tab" aria-selected={source === x.id} className={`start-source g-${x.group}${source === x.id ? " on" : ""}`}
                          onClick={() => setSource(x.id)}>
                    <span className="start-icon" aria-hidden>{x.icon}</span>
                    <span className="start-source-text"><b>{x.name}</b><span className="faint">{x.desc}</span></span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
        <div className="start-guide">
          <div className="start-guide-head">
            <span className={`start-icon g-${chosen.group}`} aria-hidden>{chosen.icon}</span>
            <div><b>Set up {chosen.name}</b><div className="start-tags">{chosen.signals.map((s) => <span key={s} className={`start-tag t-${s.toLowerCase()}`}>{s}</span>)}</div></div>
          </div>
          {guide(source, endpoint, k, site)}
        </div>
      </Step>

      <Step n={3} done={done[2]} title="See your data" hint="This updates by itself: no need to reload." last>
        <Check ok={received} pending="Waiting for your first data…"
               done={`Leasyd has received ${fmtNum(today)} records today${arrived ? ` (${fmtNum(today - (baseline ?? 0))} since you opened this page)` : ""}.`} />
        <Check ok={found} pending="Waiting for it to be searchable (usually within a minute of arriving)…"
               done={<>Searchable now, from {allServices.length} service{allServices.length === 1 ? "" : "s"}: {allServices.map((s) => <code key={s} className="mono start-svc">{s}</code>)}</>} />
        {found && (
          <div className="start-success">
            <b>You're all set.</b>
            <div className="start-row">
              {services.traces?.length ? <button className="btn primary" onClick={() => ctx.go("/services")}>Open the Service Map</button> : null}
              {services.traces?.length ? <button className="btn" onClick={() => ctx.go("/traces")}>Traces</button> : null}
              {services.logs?.length ? <button className="btn" onClick={() => ctx.go("/logs")}>Logs</button> : null}
              {services.metrics?.length ? <button className="btn" onClick={() => ctx.go("/dashboards")}>Dashboards</button> : null}
            </div>
          </div>
        )}
        {!found && (
          <p className="faint">
            Nothing yet after a few minutes? Check the key (a new key takes about 2 minutes; until then data is refused with 403),
            that the endpoint is <code className="mono">{endpoint}</code>, and your app's or collector's own log for export errors.
            Write to us at <a href="mailto:support@leasyd.com">support@leasyd.com</a> and we'll help.
          </p>
        )}
      </Step>
    </div>
  );
}

function Step({ n, done, title, hint, last, children }: { n: number; done: boolean; title: string; hint: string; last?: boolean; children: ReactNode }) {
  return (
    <section className={`start-stepcard${done ? " done" : ""}${last ? " last" : ""}`} aria-label={`Step ${n}: ${title}`}>
      <div className="start-rail" aria-hidden><span className="start-num">{done ? <IconCheck /> : n}</span></div>
      <div className="start-body">
        <h3>{title}</h3>
        <p className="faint start-hint">{hint}</p>
        <div className="start-step">{children}</div>
      </div>
    </section>
  );
}

function Check({ ok, pending, done }: { ok: boolean; pending: string; done: ReactNode }) {
  return (
    <div className={`start-check${ok ? " ok" : ""}`}>
      <span className="start-dot" aria-hidden>{ok ? "✓" : ""}</span>
      <span>{ok ? done : pending}</span>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button className="btn" onClick={() => navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function Code({ children }: { children: string }) {
  return (
    <div className="start-code">
      <pre className="code">{children}</pre>
      <CopyButton text={children} />
    </div>
  );
}

const otlpEnv = (endpoint: string, key: string, service = "my-service") => `export OTEL_SERVICE_NAME=${service}
export OTEL_EXPORTER_OTLP_ENDPOINT=${endpoint}
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_HEADERS=x-api-key=${key}`;

function guide(source: Source, endpoint: string, key: string, site: string): ReactNode {
  switch (source) {
    case "server":
      return (
        <>
          <p>On the server (Amazon Linux 2023 or Ubuntu 22.04+, x86 or ARM), install the OpenTelemetry Collector set up for Leasyd:</p>
          <Code>{`curl -fsSL ${site}/install/collector.sh -o leasyd-collector.sh
sudo LEASYD_API_KEY=${key} LEASYD_ENDPOINT=${endpoint} bash leasyd-collector.sh`}</Code>
          <p className="muted">
            It sends the server's CPU, memory, disk, network and system logs every 30 seconds (see <b>Dashboards › Leasyd - Hosts</b>),
            and takes your apps' OpenTelemetry data on <code className="mono">localhost:4318</code> (HTTP) and <code className="mono">:4317</code> (gRPC),
            so apps on this server need no key: set <code className="mono">OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318</code>.
            Add <code className="mono">--sample-app</code> to also install a small demo shop that sends traces.
          </p>
        </>
      );
    case "kubernetes":
      return (
        <>
          <p>With <code className="mono">kubectl</code> pointing at the cluster and <code className="mono">helm</code> 3, run (add <code className="mono">--gpu</code> if it has NVIDIA GPUs):</p>
          <Code>{`curl -fsSL ${site}/install/kubernetes/leasyd-k8s.sh -o leasyd-k8s.sh
LEASYD_API_KEY=${key} LEASYD_ENDPOINT=${endpoint} bash leasyd-k8s.sh --cluster my-cluster`}</Code>
          <p className="muted">
            It installs the official OpenTelemetry Collector Helm chart twice in namespace <code className="mono">leasyd</code>: one collector per node
            (node, pod and container usage, pod logs, GPU metrics, and your apps' OpenTelemetry on each node's ports 4317 / 4318) and one for the
            cluster (pod phases, restarts, crash loops, workloads, events). The key stays in a Kubernetes Secret. See <b>Kubernetes</b> within a minute or two.
            Apps in the cluster send to their node: <code className="mono">OTEL_EXPORTER_OTLP_ENDPOINT=http://$(NODE_IP):4318</code> with
            NODE_IP from the downward API (<code className="mono">status.hostIP</code>). Remove it all: <code className="mono">bash leasyd-k8s.sh --uninstall</code>.
          </p>
        </>
      );
    case "gpu":
      return (
        <>
          <p><b>In Kubernetes</b>: install with <code className="mono">--gpu</code>. If NVIDIA's GPU Operator runs, Leasyd reads its dcgm-exporter; otherwise the installer adds dcgm-exporter on the GPU nodes:</p>
          <Code>{`curl -fsSL ${site}/install/kubernetes/leasyd-k8s.sh -o leasyd-k8s.sh
LEASYD_API_KEY=${key} LEASYD_ENDPOINT=${endpoint} bash leasyd-k8s.sh --cluster my-cluster --gpu`}</Code>
          <p><b>On a server</b> with NVIDIA GPUs (driver, Docker and NVIDIA's container toolkit installed):</p>
          <Code>{`curl -fsSL ${site}/install/collector.sh -o leasyd-collector.sh
sudo LEASYD_API_KEY=${key} LEASYD_ENDPOINT=${endpoint} bash leasyd-collector.sh --gpu`}</Code>
          <p className="muted">
            See <b>GPUs</b>: each GPU's utilization, memory, temperature, power and errors (XID, ECC, throttling, with what to do), which workload
            and team holds it, what it costs and what sits idle. Label pods <code className="mono">team=&lt;name&gt;</code> to see cost by team;
            set your own prices in <b>Settings › GPU prices</b>. Slurm: set <code className="mono">DCGM_HPC_JOB_DIR</code> to your job-mapping directory to see jobs.
          </p>
        </>
      );
    case "java":
      return (
        <>
          <p>Download the OpenTelemetry Java agent and start your app with it. No code changes:</p>
          <Code>{`curl -fsSL -o opentelemetry-javaagent.jar \\
  https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar
${otlpEnv(endpoint, key)}
java -javaagent:opentelemetry-javaagent.jar -jar your-app.jar`}</Code>
          <p className="muted">Traces, logs and JVM metrics (heap, garbage collection, threads): see <b>Dashboards › Leasyd - JVM</b>.</p>
        </>
      );
    case "nodejs":
      return (
        <>
          <p>Add OpenTelemetry's auto-instrumentation and start your app with it. No code changes:</p>
          <Code>{`npm install @opentelemetry/api @opentelemetry/auto-instrumentations-node
${otlpEnv(endpoint, key)}
export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
node --require @opentelemetry/auto-instrumentations-node/register app.js`}</Code>
          <p className="muted">Traces of HTTP, databases and more, and runtime metrics (event loop, heap): see <b>Dashboards › Leasyd - Node.js</b>.</p>
        </>
      );
    case "python":
      return (
        <>
          <p>Install OpenTelemetry's auto-instrumentation and start your app with it. No code changes:</p>
          <Code>{`pip install opentelemetry-distro opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-system-metrics
opentelemetry-bootstrap -a install
${otlpEnv(endpoint, key)}
export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true
opentelemetry-instrument python app.py`}</Code>
          <p className="muted">For gunicorn or uvicorn, put <code className="mono">opentelemetry-instrument</code> in front of their command. Runtime metrics: <b>Dashboards › Leasyd - Python</b>.</p>
        </>
      );
    case "ruby":
      return (
        <>
          <p>Add the OpenTelemetry gems to your Gemfile and run <code className="mono">bundle install</code>:</p>
          <Code>{`gem "opentelemetry-sdk"
gem "opentelemetry-exporter-otlp"
gem "opentelemetry-instrumentation-all"
gem "opentelemetry-metrics-sdk", "~> 0.19.0"
gem "opentelemetry-exporter-otlp-metrics"`}</Code>
          <p>Download Leasyd's runtime metrics (Ruby's OpenTelemetry has none of its own yet) next to your app:</p>
          <Code>{`curl -fsSL -o leasyd_runtime_metrics.rb ${site}/install/leasyd_runtime_metrics.rb`}</Code>
          <p>Configure OpenTelemetry at startup (e.g. <code className="mono">config/initializers/opentelemetry.rb</code> in Rails):</p>
          <Code>{`require "opentelemetry/sdk"
require "opentelemetry/exporter/otlp"
require "opentelemetry/instrumentation/all"
require "opentelemetry-metrics-sdk"
require "opentelemetry-exporter-otlp-metrics"
require_relative "leasyd_runtime_metrics"

OpenTelemetry::SDK.configure { |c| c.use_all }
LeasydRuntimeMetrics.start`}</Code>
          <p>And run it with:</p>
          <Code>{otlpEnv(endpoint, key)}</Code>
          <p className="muted">Runtime metrics (CPU, memory, garbage collection): see <b>Dashboards › Leasyd - Ruby</b>.</p>
        </>
      );
    case "lambda":
      return (
        <>
          <p><b>Metrics for every function</b> (invocations, errors, duration, throttles): stream them from CloudWatch. Run once per region with AWS credentials:</p>
          <Code>{`curl -fsSL -o leasyd-cloudwatch-metrics.yaml ${site}/install/cloudwatch-metrics.yaml
aws cloudformation deploy --stack-name leasyd-cloudwatch-metrics \\
  --template-file leasyd-cloudwatch-metrics.yaml --capabilities CAPABILITY_IAM \\
  --parameter-overrides LeasydApiKey=${key} LeasydEndpoint=${endpoint}/v1/aws/cloudwatch-metrics`}</Code>
          <p className="muted">
            Functions appear under <b>AWS Lambda</b> within a few minutes. AWS bills the stream (about $0.003 per 1,000 metric updates,
            roughly 8 a minute per function) and Firehose. Add <code className="mono">ExtraNamespace1=AWS/SQS</code> (or another namespace) to send more.
          </p>
          <p><b>Each invocation, with its logs</b>: add the OpenTelemetry Lambda layer for your runtime to the function, and set (Java: <code className="mono">/opt/otel-handler</code>):</p>
          <Code>{`AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument
OTEL_EXPORTER_OTLP_ENDPOINT=${endpoint}
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=${key}`}</Code>
          <p className="muted">
            To see the invocation page first, deploy our sample function (invoked 5 times a minute, fails now and then):{" "}
            <code className="mono">curl -fsSLO {site}/install/lambda-sample.yaml</code>, then the same <code className="mono">aws cloudformation deploy</code>{" "}
            with <code className="mono">--stack-name leasyd-lambda-sample</code>. Delete the stacks to stop.
          </p>
        </>
      );
    case "aws-logs":
      return (
        <>
          <p>Send the logs already in <b>CloudWatch Logs</b> (Lambda, ECS, RDS, API Gateway, your own groups). List the log groups, then run once per region with AWS credentials:</p>
          <Code>{`curl -fsSL -o leasyd-cloudwatch-logs.yaml ${site}/install/cloudwatch-logs.yaml
aws cloudformation deploy --stack-name leasyd-cloudwatch-logs \\
  --template-file leasyd-cloudwatch-logs.yaml --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \\
  --parameter-overrides LeasydApiKey=${key} LeasydEndpoint=${endpoint}/v1/aws/cloudwatch-logs \\
    LogGroups=/aws/lambda/my-function,/ecs/my-service`}</Code>
          <p className="muted">
            New log events appear under <b>Logs</b> about a minute after they're written, each under its service: a Lambda function, an ECS container,
            an RDS database, or the log group's name. <code className="mono">AllLogGroups=true</code> sends every log group in the region instead;
            {" "}<code className="mono">FilterPattern=&quot;?ERROR ?WARN&quot;</code> sends only those lines. AWS bills Firehose (about $0.029 per GB). Delete the stack to stop.
          </p>
        </>
      );
    case "web":
      return <WebGuide />;
    case "other":
      return (
        <>
          <p>Every OpenTelemetry SDK reads these settings. Set them where your app starts:</p>
          <Code>{otlpEnv(endpoint, key)}</Code>
          <p className="muted">
            Leasyd takes OTLP over HTTP (protobuf or JSON), not gRPC: point gRPC-only exporters at a Collector
            (the Linux server option) instead. Setup per language: <a href="https://opentelemetry.io/docs/languages/" target="_blank" rel="noreferrer">opentelemetry.io/docs/languages</a>.
          </p>
        </>
      );
  }
}

/** Web monitoring: a public browser key for the site's origins (not the API key above), then one script tag. */
function WebGuide() {
  const [origins, setOrigins] = useState("");
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true); setError(null);
    try { setKey((await account.createKey("browser", origins.split(/[\s,]+/).filter(Boolean))).api_key); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <>
      <p>Real users' page loads, Core Web Vitals (LCP, INP, CLS), JavaScript errors and slow or failed API calls, from a small
        script on your site (about 4 KB gzipped, no cookies, URLs sent without query strings). It needs a <b>browser key</b>:
        public by design, it can only send web events, and only from the sites you list.</p>
      {key ? <>
        <p>Add this to every page, before <code className="mono">&lt;/head&gt;</code> (set <code className="mono">data-app</code> to your site's name):</p>
        <Code>{rumSnippet(key)}</Code>
        <p className="muted">Calls from the page to its own origin carry a trace header, so a slow page links to the backend trace; list other
          API origins in <code className="mono">data-trace-origins</code> (they must accept the <code className="mono">traceparent</code> header).
          Then see <b>Explore › Web</b>.</p>
      </> : <>
        <div className="start-row">
          <input className="input grow" value={origins} onChange={(e) => setOrigins(e.target.value)}
            placeholder="Your site, e.g. https://shop.example.com (several: comma-separated)" aria-label="Your site's address" />
          <button className="btn primary" disabled={busy || !origins.trim()} onClick={create}>{busy ? "Creating…" : "Create browser key"}</button>
        </div>
        {error && <div className="form-error" role="alert">{error}</div>}
        <p className="faint">Owners create keys; others can find browser keys in Settings › API keys.</p>
      </>}
    </>
  );
}
