// A small Node.js web service to try Leasyd's Node.js monitoring: run it with OpenTelemetry's
// auto-instrumentation (run.sh) and it sends its traces, logs and runtime metrics (event loop
// delay and utilization, V8 heap, garbage collection). It calls itself a few times a second, so
// there is always traffic; now and then it blocks the event loop, as real apps do.
const http = require("http");

const port = Number(process.env.PORT || 3000);
const cache = [];   // keeps some memory live, so the heap grows and gets collected

function busy(ms) {                       // synchronous work: blocks the event loop meanwhile
  const end = Date.now() + ms;
  let x = 0;
  while (Date.now() < end) x += Math.sqrt(Math.random());
  return x;
}

const server = http.createServer((req, res) => {
  cache.push(Buffer.alloc(32 * 1024 + Math.floor(Math.random() * 128 * 1024)));
  if (cache.length > 300) cache.splice(0, 50);
  if (req.url === "/report") {
    busy(80 + Math.random() * 250);       // a slow report: everything else waits for it
    console.log("report built");
    res.writeHead(200, { "content-type": "application/json" }).end('{"rows":120}');
    return;
  }
  setTimeout(() => {
    if (Math.random() < 0.02) {
      console.error("inventory lookup failed: upstream timeout");
      res.writeHead(503, { "content-type": "application/json" }).end('{"error":"upstream timeout"}');
    } else {
      busy(2 + Math.random() * 6);
      res.writeHead(200, { "content-type": "application/json" }).end('{"in_stock":true}');
    }
  }, 5 + Math.random() * 40);
});

server.listen(port, () => console.log(`inventory-api listening on :${port}`));

const paths = ["/stock", "/stock", "/stock", "/stock", "/stock", "/report"];
function tick() {
  const path = paths[Math.floor(Math.random() * paths.length)];
  http.get({ port, path }, (r) => r.resume()).on("error", (e) => console.warn("call failed:", e.message));
  setTimeout(tick, 150 + Math.random() * 300);
}
tick();
