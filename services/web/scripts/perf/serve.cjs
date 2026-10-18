// Serves a production build with its mock backend (an ES-module bundle of mock/mockApi.ts, with
// mock/shot.jpg beside it): node serve.cjs <mock.mjs> <dist> <port>
const http = require("http"), fs = require("fs"), path = require("path");
const [mockFile, dist, port] = process.argv.slice(2);
(async () => {
const mw = [];
(await import(mockFile)).mockApi().configureServer({ middlewares: { use: (m) => mw.push(m) } });
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png" };
http.createServer((req, res) => {
  let i = 0;
  const next = () => {
    const m = mw[i++];
    if (m) return m(req, res, next);
    const p = path.join(dist, decodeURIComponent(req.url.split("?")[0]));
    const file = fs.existsSync(p) && fs.statSync(p).isFile() ? p : path.join(dist, "index.html");
    res.setHeader("content-type", TYPES[path.extname(file)] || "application/octet-stream");
    fs.createReadStream(file).pipe(res);
  };
  next();
}).listen(Number(port));
})();
