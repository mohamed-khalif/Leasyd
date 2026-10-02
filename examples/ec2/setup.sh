#!/usr/bin/env bash
# Sends an EC2 server's telemetry to Leasyd: installs the OpenTelemetry Collector (contrib) as a
# service that
#   - collects the server's own metrics (CPU, memory, disk, network, load, processes) every 30 s
#     and its system logs (journald),
#   - receives your applications' traces, metrics and logs on localhost:4317 (gRPC) and :4318 (HTTP),
#   - adds the EC2 details (instance id, type, region, ...) and forwards everything to Leasyd.
# Optional --sample-app also installs a small two-service shop (Python, auto-instrumented) with
# steady traffic, so you see traces even before your own app is instrumented.
#
#   sudo LEASYD_API_KEY=obs_... bash setup.sh [--sample-app]
#
# Amazon Linux 2023 or Ubuntu 22.04+/24.04, x86_64 or arm64. Safe to re-run (to change the key too).
set -euo pipefail
: "${LEASYD_API_KEY:?set LEASYD_API_KEY to an API key that sends data (Leasyd > Settings > API keys)}"
LEASYD_ENDPOINT="${LEASYD_ENDPOINT:-https://ingest.leasyd.com}"
HOST_SERVICE="${HOST_SERVICE:-ec2-host}"      # service name for the server's own metrics and logs
OTELCOL_VERSION="${OTELCOL_VERSION:-0.161.0}"
SAMPLE_APP=false; [[ "${1:-}" == "--sample-app" ]] && SAMPLE_APP=true
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "unsupported CPU $(uname -m)" >&2; exit 1 ;; esac
BASE="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}"
if command -v dnf >/dev/null; then PKG=rpm; else PKG=deb; fi

if ! command -v otelcol-contrib >/dev/null || [[ "$(otelcol-contrib --version | awk '{print $3}')" != "$OTELCOL_VERSION" ]]; then
  echo "== installing the OpenTelemetry Collector ${OTELCOL_VERSION}"
  f="/tmp/otelcol-contrib.${PKG}"
  curl -fsSL -o "$f" "${BASE}/otelcol-contrib_${OTELCOL_VERSION}_linux_${ARCH}.${PKG}"
  if [[ $PKG == rpm ]]; then rpm -U --replacepkgs "$f"; else dpkg -i "$f"; fi
  rm -f "$f"
fi
# Reading the system journal needs this group.
getent group systemd-journal >/dev/null && usermod -aG systemd-journal otelcol-contrib || true

echo "== writing /etc/otelcol-contrib/config.yaml"
cat > /etc/otelcol-contrib/config.yaml <<'YAML'
receivers:
  otlp:                               # your applications send here
    protocols:
      grpc: { endpoint: localhost:4317 }
      http: { endpoint: localhost:4318 }
  hostmetrics:                        # the server itself
    collection_interval: 30s
    scrapers:
      cpu:
        metrics: { system.cpu.utilization: { enabled: true } }
      memory:
        metrics: { system.memory.utilization: { enabled: true } }
      disk: {}
      filesystem:
        exclude_fs_types: { fs_types: [tmpfs, devtmpfs, overlay, squashfs, autofs, proc, sysfs, cgroup2], match_type: strict }
      network: {}
      load: {}
      paging: {}
      processes: {}
  journald:                           # system logs
    priority: info

processors:
  memory_limiter: { check_interval: 1s, limit_percentage: 80, spike_limit_percentage: 25 }
  resource_detection:                 # host.name, cloud.region, host.id (instance id), host.type, ...
    detectors: [env, ec2, system]
    timeout: 5s
    override: false
  resource/host:                      # the server's own data appears under this service
    attributes:
      - { key: service.name, value: "${env:HOST_SERVICE}", action: insert }
  batch: { timeout: 5s }

exporters:
  otlphttp/leasyd:                    # Leasyd takes OTLP over HTTP (not gRPC)
    endpoint: ${env:LEASYD_ENDPOINT}
    headers: { x-api-key: "${env:LEASYD_API_KEY}" }
    compression: gzip

service:
  pipelines:
    traces:       { receivers: [otlp],        processors: [memory_limiter, resource_detection, batch],                exporters: [otlphttp/leasyd] }
    metrics:      { receivers: [otlp],        processors: [memory_limiter, resource_detection, batch],                exporters: [otlphttp/leasyd] }
    logs:         { receivers: [otlp],        processors: [memory_limiter, resource_detection, batch],                exporters: [otlphttp/leasyd] }
    metrics/host: { receivers: [hostmetrics], processors: [memory_limiter, resource_detection, resource/host, batch], exporters: [otlphttp/leasyd] }
    logs/host:    { receivers: [journald],    processors: [memory_limiter, resource_detection, resource/host, batch], exporters: [otlphttp/leasyd] }
YAML
# The key stays out of the config file, in the service's environment file (readable by root only).
cat > /etc/otelcol-contrib/otelcol-contrib.conf <<CONF
OTELCOL_OPTIONS="--config=/etc/otelcol-contrib/config.yaml"
LEASYD_API_KEY=${LEASYD_API_KEY}
LEASYD_ENDPOINT=${LEASYD_ENDPOINT}
HOST_SERVICE=${HOST_SERVICE}
CONF
chmod 600 /etc/otelcol-contrib/otelcol-contrib.conf
systemctl enable --now otelcol-contrib >/dev/null
systemctl restart otelcol-contrib

if $SAMPLE_APP; then
  echo "== installing the sample shop (services shop-api and inventory) and its traffic"
  APP=/opt/leasyd-sample
  mkdir -p "$APP"
  if [[ $PKG == rpm ]]; then dnf install -y -q python3 python3-pip >/dev/null; else apt-get update -qq && apt-get install -y -qq python3-venv python3-pip >/dev/null; fi
  python3 -m venv "$APP/venv"
  "$APP/venv/bin/pip" install -q flask requests opentelemetry-distro opentelemetry-exporter-otlp-proto-http
  "$APP/venv/bin/opentelemetry-bootstrap" -a install >/dev/null
  cat > "$APP/app.py" <<'PY'
"""A small two-service shop: shop-api (port 8080) calls inventory (port 8081); both use SQLite.
Some requests are slow and about 3% fail, so there is something to find."""
import logging, os, random, sqlite3, time
import requests
from flask import Flask, abort, jsonify

ROLE = os.environ.get("ROLE", "shop-api")
app = Flask(ROLE)
log = logging.getLogger(ROLE)
logging.basicConfig(level=logging.INFO)
DB = f"/opt/leasyd-sample/{ROLE}.db"

def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT, stock INTEGER)")
    if not con.execute("SELECT count(*) FROM items").fetchone()[0]:
        con.executemany("INSERT INTO items (name, stock) VALUES (?, ?)",
                        [(n, random.randint(0, 50)) for n in ("kettle", "mug", "teapot", "grinder", "scale", "filter")])
        con.commit()
    return con

@app.get("/health")
def health():
    return "ok"

if ROLE == "inventory":
    @app.get("/stock/<int:item>")
    def stock(item):
        time.sleep(random.choice([0.005, 0.01, 0.02, 0.4]) if random.random() < 0.1 else 0.005)
        row = db().execute("SELECT name, stock FROM items WHERE id = ?", (item,)).fetchone()
        if not row:
            abort(404)
        return jsonify(name=row[0], stock=row[1])
else:
    @app.get("/products")
    def products():
        rows = db().execute("SELECT id, name FROM items").fetchall()
        return jsonify([{"id": i, "name": n} for i, n in rows])

    @app.post("/checkout/<int:item>")
    def checkout(item):
        s = requests.get(f"http://localhost:8081/stock/{item}", timeout=5)
        if s.status_code != 200:
            log.warning("checkout of unknown item %s", item)
            abort(404)
        if random.random() < 0.03:
            log.error("payment provider timed out for item %s", item)
            abort(502)
        log.info("order placed for %s", s.json()["name"])
        return jsonify(ok=True)
PY
  cat > "$APP/traffic.sh" <<'SH'
#!/usr/bin/env bash
# Steady traffic: a few requests a second.
while true; do
  curl -s -o /dev/null localhost:8080/products
  curl -s -o /dev/null -X POST "localhost:8080/checkout/$((RANDOM % 7 + 1))"
  sleep 0.$((RANDOM % 9 + 1))
done
SH
  chmod +x "$APP/traffic.sh"
  for svc in shop-api:8080 inventory:8081; do
    name="${svc%%:*}"; port="${svc##*:}"
    cat > "/etc/systemd/system/leasyd-${name}.service" <<UNIT
[Unit]
Description=Leasyd sample app: ${name}
After=network-online.target otelcol-contrib.service
[Service]
WorkingDirectory=${APP}
Environment=ROLE=${name}
Environment=OTEL_SERVICE_NAME=${name}
Environment=OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
Environment=OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
Environment=OTEL_TRACES_EXPORTER=otlp
Environment=OTEL_METRICS_EXPORTER=otlp
Environment=OTEL_LOGS_EXPORTER=otlp
Environment=OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true
ExecStart=${APP}/venv/bin/opentelemetry-instrument ${APP}/venv/bin/flask --app app run --host 127.0.0.1 --port ${port}
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  done
  cat > /etc/systemd/system/leasyd-traffic.service <<UNIT
[Unit]
Description=Leasyd sample app: traffic
After=leasyd-shop-api.service leasyd-inventory.service
[Service]
ExecStart=${APP}/traffic.sh
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now leasyd-inventory leasyd-shop-api leasyd-traffic >/dev/null
  systemctl restart leasyd-inventory leasyd-shop-api leasyd-traffic
fi

sleep 5
if systemctl is-active --quiet otelcol-contrib; then
  echo
  echo "Done. The collector is running and sending to ${LEASYD_ENDPOINT}."
  echo "Data shows up in Leasyd within a minute or two: Metrics (service ${HOST_SERVICE}), Logs$($SAMPLE_APP && echo ", Traces (shop-api, inventory)")."
  echo "Its own log: sudo journalctl -u otelcol-contrib -f"
else
  echo "The collector did not start; see: sudo journalctl -u otelcol-contrib -n 50" >&2
  exit 1
fi
