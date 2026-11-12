// Проверяет собранный index.html:
//  - каждая ссылка #якорь ведёт на существующий id (или на старый якорь из architecture/aliases.json);
//  - каждая ссылка api.html#tag/<тег>/<МЕТОД><путь> есть в api/openapi.json — только если файл спецификации существует;
//  - каждый блок ```mermaid непустой.
// Запуск: node tools/check.mjs   (выполняется также в npm run build)
import fs from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const doc = new JSDOM(html).window.document;
const aliases = JSON.parse(fs.readFileSync(path.join(ROOT, "architecture/aliases.json"), "utf8"));
const specPath = path.join(ROOT, "api/openapi.json");
const spec = fs.existsSync(specPath) ? JSON.parse(fs.readFileSync(specPath, "utf8")) : null;

// Раздел → .md-файл, чтобы сообщение указывало, что править.
const fileOf = {};
for (const dir of fs.readdirSync(path.join(ROOT, "architecture"))) {
  const abs = path.join(ROOT, "architecture", dir);
  if (!fs.statSync(abs).isDirectory()) continue;
  for (const f of fs.readdirSync(abs)) {
    const m = fs.readFileSync(path.join(abs, f), "utf8").match(/^id:\s*"?([^"\n]+)"?/m);
    if (m) fileOf[m[1]] = `architecture/${dir}/${f}`;
  }
}

// Как Scalar строит якоря: tag/<тег-в-нижнем-регистре-через-дефис>/<МЕТОД><путь>.
const slug = (s) => s.toLowerCase().replace(/\s+/g, "-");
const ops = new Set();
const tags = new Set();
for (const [p, item] of Object.entries(spec?.paths || {})) {
  for (const [method, op] of Object.entries(item)) {
    if (typeof op !== "object" || !op) continue;
    for (const t of op.tags || []) {
      tags.add(slug(t));
      ops.add(`tag/${slug(t)}/${method.toUpperCase()}${p}`);
    }
  }
}

const ids = new Set([...doc.querySelectorAll("[id]")].map((e) => e.id));
const errors = [];
const whereOf = (node) => {
  const sec = node.closest("section[id]");
  return sec ? fileOf[sec.id] || sec.id : "меню / оболочка (architecture/_shell.html или front matter)";
};
for (const a of doc.querySelectorAll("a[href]")) {
  const href = a.getAttribute("href");
  const where = whereOf(a);
  const text = a.textContent.replace(/\s+/g, " ").trim();
  if (href.startsWith("api.html#")) {
    if (!spec) {
      errors.push(`${where}: «${text}» → ${href} — нет api/openapi.json для проверки`);
      continue;
    }
    const anchor = decodeURIComponent(href.slice("api.html#".length));
    const tagOnly = anchor.match(/^tag\/([^/]+)$/);
    if (tagOnly ? !tags.has(tagOnly[1]) : !ops.has(anchor)) {
      errors.push(`${where}: «${text}» → ${href} — такой операции нет в api/openapi.json`);
    }
  } else if (href.startsWith("#") && href.length > 1) {
    const id = decodeURIComponent(href.slice(1));
    if (!ids.has(id) && !aliases[id]) errors.push(`${where}: «${text}» → ${href} — такого якоря нет`);
  }
}
for (const pre of doc.querySelectorAll("pre.mermaid")) {
  if (!pre.textContent.trim()) errors.push(`${whereOf(pre)}: пустой блок mermaid`);
}

if (errors.length) {
  console.error(`check: ${errors.length} ошибок\n` + errors.map((e) => "  ✗ " + e).join("\n"));
  process.exit(1);
}
const api = spec ? `${ops.size} операций API, ` : "";
console.log(`check: OK — ${api}${ids.size} якорей, ${doc.querySelectorAll("pre.mermaid").length} диаграмм, ссылки целы`);
