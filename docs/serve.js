#!/usr/bin/env node
// Локальный сервер документации Telebrief: раздаёт эту папку и сохраняет комментарии в comments.json.
//
// Запуск:   node serve.js [порт]      (по умолчанию 8766)
// Открыть: http://localhost:8766/
// Другой файл комментариев: COMMENTS_FILE=/путь/comments.json node serve.js

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = __dirname;
const STORE = path.resolve(process.env.COMMENTS_FILE || path.join(ROOT, "comments.json"));
const API = "/api/comments";
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".pdf": "application/pdf",
  ".map": "application/json; charset=utf-8",
};

function send(res, code, body, type = "application/json; charset=utf-8") {
  res.writeHead(code, { "Content-Type": type, "Cache-Control": "no-store", "Content-Length": Buffer.byteLength(body) });
  res.end(body);
}

function readComments(res) {
  fs.readFile(STORE, (err, buf) => {
    if (err && err.code === "ENOENT") return send(res, 200, '{"comments": []}');
    if (err) return send(res, 500, '{"error": "read failed"}');
    send(res, 200, buf);
  });
}

function writeComments(req, res) {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    let data;
    try {
      data = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch {
      data = null;
    }
    if (!data || typeof data !== "object" || !Array.isArray(data.comments)) {
      return send(res, 400, '{"error": "expected {\\"comments\\": [...]}"}');
    }
    // Атомарная запись: временный файл в той же папке, затем замена.
    const tmp = path.join(path.dirname(STORE), `.comments-${process.pid}-${Date.now()}.tmp`);
    try {
      fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n");
      fs.renameSync(tmp, STORE);
    } catch {
      fs.rmSync(tmp, { force: true });
      return send(res, 500, '{"error": "write failed"}');
    }
    send(res, 200, '{"ok": true}');
  });
}

function serveStatic(req, res, pathname) {
  let file;
  try {
    file = path.join(ROOT, path.normalize(decodeURIComponent(pathname)));
  } catch {
    return send(res, 400, "Bad Request", "text/plain; charset=utf-8");
  }
  if (file !== ROOT && !file.startsWith(ROOT + path.sep)) {
    return send(res, 403, "Forbidden", "text/plain; charset=utf-8");
  }
  fs.stat(file, (err, st) => {
    if (!err && st.isDirectory()) {
      if (!pathname.endsWith("/")) {
        res.writeHead(301, { Location: pathname + "/" });
        return res.end();
      }
      file = path.join(file, "index.html");
    }
    fs.readFile(file, (err2, buf) => {
      if (err2) return send(res, 404, "Not Found", "text/plain; charset=utf-8");
      const type = MIME[path.extname(file).toLowerCase()] || "application/octet-stream";
      res.writeHead(200, { "Content-Type": type, "Content-Length": buf.length });
      res.end(req.method === "HEAD" ? undefined : buf);
    });
  });
}

const server = http.createServer((req, res) => {
  const { pathname } = new URL(req.url, "http://localhost");
  if (pathname === API) {
    if (req.method === "GET") return readComments(res);
    if (req.method === "PUT") return writeComments(req, res);
    return send(res, 405, '{"error": "method not allowed"}');
  }
  if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, "Method Not Allowed", "text/plain; charset=utf-8");
  serveStatic(req, res, pathname);
});

const port = Number(process.argv[2]) || 8766;
server.listen(port, "127.0.0.1", () => {
  console.log(`Telebrief:    http://localhost:${port}/`);
  console.log(`Комментарии:  ${STORE}`);
});
