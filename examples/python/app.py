"""A small Python web service to try Leasyd's Python monitoring: run it with OpenTelemetry's
auto-instrumentation (run.sh) and it sends its traces, logs and runtime metrics (CPU, memory,
garbage collection, threads, context switches). It calls itself a few times a second, so there
is always traffic."""
import logging
import os
import random
import threading
import time

import requests
from flask import Flask, jsonify

app = Flask(__name__)
log = logging.getLogger("pricing")
PORT = int(os.environ.get("PORT", "5000"))
cache = []   # keeps some objects alive, so the garbage collector has work to do


@app.get("/price/<sku>")
def price(sku):
    cache.append([{"sku": sku, "n": i} for i in range(random.randint(200, 2000))])
    del cache[:-200]
    time.sleep(random.uniform(0.005, 0.04))
    if random.random() < 0.02:
        log.error("price lookup failed for %s: rates service timed out", sku)
        return jsonify(error="rates service timed out"), 504
    return jsonify(sku=sku, price=round(random.uniform(3, 90), 2))


@app.get("/quote")
def quote():
    total = sum(requests.get(f"http://127.0.0.1:{PORT}/price/sku-{random.randint(1, 50)}", timeout=5).status_code == 200
                for _ in range(3))
    log.info("quote built from %d prices", total)
    return jsonify(prices=total)


def traffic():
    time.sleep(2)
    while True:
        try:
            requests.get(f"http://127.0.0.1:{PORT}/quote", timeout=10)
        except requests.RequestException as e:
            log.warning("call failed: %s", e)
        time.sleep(random.uniform(0.2, 0.6))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=traffic, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
