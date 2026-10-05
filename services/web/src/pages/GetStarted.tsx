// Get started: from an empty account to data in Leasyd. 1. an API key (create one, shown once,
// or paste one you have); 2. what to monitor: copy-paste setup for a server (the Collector), Java,
// Node.js, Python, Ruby, any other OpenTelemetry SDK, or AWS Lambda, with the key and endpoint filled
// in; 3. a live check: what Leasyd has received today (the account's meter) and which services are
// searchable yet (a count by service over the last 15 minutes, until some show up).
// Install files (collector.sh, the Ruby runtime metrics, the AWS templates) are served by the web app
// itself under /install/ (infra/deploy-phaseW1.sh copies them from examples/).
import { ReactNode, useEffect, useMemo, useState } from "react";
import { account, Account, query, records } from "../api";
import type { Ctx } from "../App";
import { Panel } from "../components/Panel";
import { getConfig } from "../config";
import { fmtNum } from "../time";

type Source = "server" | "java" | "nodejs" | "python" | "ruby" | "other" | "lambda";
const SOURCES: [Source, string, string][] = [
  ["server", "Linux server", "Host metrics and system logs, and a local endpoint for your apps"],
  ["java", "Java", "OpenTelemetry Java agent: traces, logs, JVM metrics"],
  ["nodejs", "Node.js", "Auto-instrumentation: traces, runtime metrics"],
  ["python", "Python", "Auto-instrumentation: traces, logs, runtime metrics"],
  ["ruby", "Ruby", "OpenTelemetry SDK plus Leasyd's runtime metrics"],
  ["lambda", "AWS Lambda", "CloudWatch metrics for every function, plus traces"],
  ["other", "Other (.NET, Go, PHP…)", "Any OpenTelemetry SDK, over OTLP/HTTP"],
];
const SIGNALS = ["traces", "logs", "metrics"] as const;
const POLL_MS = 10_000;

export function GetStarted({ ctx }: { ctx: Ctx }) {
  const [acc, setAcc] = useState<Account | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [created, setCreated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<Source>("server");
  const [baseline, setBaseline] = useState<number | null>(null);
  const [services, setServices] = useState<Record<string, string[]>>({});

  const endpoint = (getConfig().ingestUrl || "https://ingest.leasyd.com").replace(/\/$/, "");
  const site = location.origin;
  const k = key.trim() || "<your API key>";

  // What Leasyd has received today: the account's meter, every POLL_MS.
  useEffect(() => {
    let live = true;
    const load = () => account.get().then((a) => {
      if (!live) return;
      setAcc(a);
      setBaseline((b) => (b == null ? a.today?.records ?? 0 : b));
    }, (e: Error) => live && setError(e.message));
    load();
    const t = setInterval(load, POLL_MS);
    return () => { live = false; clearInterval(t); };
  }, []);

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
  const allServices = useMemo(() => [...new Set(SIGNALS.flatMap((s) => services[s] ?? []))].sort(), [services]);

  return (
    <div className="start">
      <div className="start-head">
        <h2>Send your first data</h2>
        <p className="muted">Three steps, about five minutes. Leasyd takes OpenTelemetry data (logs, traces and metrics) and AWS CloudWatch metrics.</p>
      </div>
      {error && <div className="form-error" role="alert">{error}</div>}

      <Panel title="1. An API key">
        <div className="start-step">
          {created ? (
            <div className="newkey" style={{ borderRadius: 6 }}>
              <b>Your new key: copy it now, it won't be shown again.</b>
              <span className="faint">It starts working within about 2 minutes. It is filled in below.</span>
              <div className="newkey-row"><code className="mono">{key}</code><CopyButton text={key} /></div>
            </div>
          ) : (
            <>
              <p className="muted">Your apps send data with a key that can only send, never read.</p>
              <div className="start-row">
                {owner
                  ? <button className="btn primary" onClick={createKey} disabled={busy}>{busy ? "Creating…" : "Create a key"}</button>
                  : acc && <span className="faint">Only an owner of the account can create keys; ask one, or paste a key you have.</span>}
                <span className="faint">or paste one you have:</span>
                <input className="input mono grow" placeholder="obs_…" value={key} onChange={(e) => setKey(e.target.value)}
                       aria-label="Your API key" autoComplete="off" spellCheck={false} />
              </div>
            </>
          )}
        </div>
      </Panel>

      <Panel title="2. What do you want to monitor?">
        <div className="start-step">
          <div className="start-sources" role="tablist">
            {SOURCES.map(([id, name, desc]) => (
              <button key={id} role="tab" aria-selected={source === id} className={`start-source${source === id ? " on" : ""}`} onClick={() => setSource(id)}>
                <b>{name}</b><span className="faint">{desc}</span>
              </button>
            ))}
          </div>
          <div className="start-guide">{guide(source, endpoint, k, site)}</div>
        </div>
      </Panel>

      <Panel title="3. Check it works">
        <div className="start-step">
          <Check ok={arrived || today > 0} pending="Waiting for your first data…"
                 done={`Leasyd has received ${fmtNum(today)} records today${arrived ? ` (${fmtNum(today - (baseline ?? 0))} since you opened this page)` : ""}.`} />
          <Check ok={found} pending="Waiting for it to be searchable (usually within a minute of arriving)…"
                 done={<>Searchable now: {allServices.map((s) => <code key={s} className="mono start-svc">{s}</code>)}</>} />
          {found && (
            <div className="start-row">
              {services.traces?.length ? <button className="btn primary" onClick={() => ctx.go("/services")}>Open the Service Map</button> : null}
              {services.traces?.length ? <button className="btn" onClick={() => ctx.go("/traces")}>Traces</button> : null}
              {services.logs?.length ? <button className="btn" onClick={() => ctx.go("/logs")}>Logs</button> : null}
              {services.metrics?.length ? <button className="btn" onClick={() => ctx.go("/dashboards")}>Dashboards</button> : null}
            </div>
          )}
          {!found && (
            <p className="faint">
              Nothing yet after a few minutes? Check the key (a new key takes about 2 minutes; until then data is refused with 403),
              that the endpoint is <code className="mono">{endpoint}</code>, and your app's or collector's own log for export errors.
              Write to us at <a href="mailto:support@leasyd.com">support@leasyd.com</a> and we'll help.
            </p>
          )}
        </div>
      </Panel>
    </div>
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
