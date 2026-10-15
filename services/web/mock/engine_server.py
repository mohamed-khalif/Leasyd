"""The web app's local preview of Kubernetes and GPU data: Leasyd's real query engine (in-process,
services/compaction/localengine.py) over the demo cluster (services/demo/cluster.py) for the last
HOURS hours. mock/mockApi.ts sends it the SQL that reads Kubernetes or GPU data.

    python3 mock/engine_server.py [HOURS=6]      # then: npx vite --mode mock
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "..", "compaction"), os.path.join(HERE, "..", "..", "demo")]

from localengine import LocalEngine, load_minutes  # noqa: E402
import cluster  # noqa: E402

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 6
eng = LocalEngine()
now = int(time.time() // 60)
t0 = time.time()


def docs(m):
    d = cluster.documents(m)
    return {"metrics": {"resourceMetrics": d["metrics"]}, "logs": {"resourceLogs": d["logs"]}}


load_minutes(eng, docs, range(now - int(HOURS * 60), now))
print(f"loaded {HOURS} h of the demo cluster in {time.time() - t0:.0f} s; serving on 127.0.0.1:5299", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        q = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            out, status = eng.sql(q["sql"], q["start"], q["end"], services=q.get("services")), 200
        except Exception as e:   # noqa: BLE001  (a preview: say what went wrong)
            out, status = {"error": str(e)}, 400
        body = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer((   # one query at a time: the in-process engine (moto, DuckDB) isn't shared across threads
    "127.0.0.1", 5299), Handler).serve_forever()
