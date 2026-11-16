// Static server for the candidate production build (`frontend/dist`) with an
// `/api` reverse proxy to a local uvicorn — the running-stack shape nginx
// provides in deployment, reproduced locally so the E2E suite exercises the
// real bundle against the real API (docs/47 §14: "the running stack, not
// TestClient"). Run standalone:  node harness/serve.mjs [port] [apiPort]
// or import { startServer } from "./harness/serve.mjs".
import http from "node:http";
import { createReadStream, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_DIST = path.resolve(HERE, "../../../dist");

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".webmanifest": "application/manifest+json",
  ".map": "application/json; charset=utf-8",
};

export function startServer({ port = 8100, apiPort = 8000, dist = DEFAULT_DIST, host = "127.0.0.1" } = {}) {
  if (!existsSync(path.join(dist, "index.html"))) {
    throw new Error(`no built bundle at ${dist} — run \`npm run build\` in frontend/ first`);
  }

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${host}:${port}`);
    if (url.pathname.startsWith("/api/")) {
      proxy(req, res, apiPort, url);
      return;
    }
    staticFile(req, res, dist, url.pathname);
  });

  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(port, host, () => {
      resolve({
        server,
        url: `http://${host}:${port}`,
        close: () => new Promise((r) => server.close(r)),
      });
    });
  });
}

function proxy(req, res, apiPort, url) {
  const upstream = http.request(
    {
      host: "127.0.0.1",
      port: apiPort,
      method: req.method,
      path: url.pathname + url.search,
      headers: { ...req.headers, host: `127.0.0.1:${apiPort}` },
    },
    (up) => {
      res.writeHead(up.statusCode || 502, up.headers);
      up.pipe(res);
    },
  );
  upstream.on("error", (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "application/json" });
    }
    res.end(JSON.stringify({ detail: `e2e proxy: ${err.message}`, code: "e2e_proxy_unavailable" }));
  });
  req.pipe(upstream);
}

function staticFile(req, res, dist, pathname) {
  const rel = decodeURIComponent(pathname).replace(/^\/+/, "");
  let file = path.join(dist, rel);
  // Contain the path inside dist (SPA fallback must not read outside it).
  if (!file.startsWith(dist)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  if (!existsSync(file) || statSync(file).isDirectory()) {
    file = path.join(dist, "index.html"); // SPA history fallback
  }
  const type = TYPES[path.extname(file).toLowerCase()] || "application/octet-stream";
  const stat = statSync(file);
  res.writeHead(200, {
    "content-type": type,
    "content-length": stat.size,
    "cache-control": "no-store",
  });
  createReadStream(file).pipe(res);
}

if (process.argv[1] && import.meta.url === `file://${path.resolve(process.argv[1])}`) {
  const port = Number(process.argv[2] || 8100);
  const apiPort = Number(process.argv[3] || 8000);
  const started = await startServer({ port, apiPort });
  console.log(`serving ${DEFAULT_DIST} on ${started.url} -> /api -> 127.0.0.1:${apiPort}`);
}
