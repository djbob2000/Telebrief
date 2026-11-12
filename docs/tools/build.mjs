// Собирает index.html из architecture/**/*.md и architecture/_shell.html.
// Запуск: node tools/build.mjs   (или npm run build — сборка + проверка ссылок)
import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";
import MarkdownIt from "markdown-it";
import attrs from "markdown-it-attrs";
import container from "markdown-it-container";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const DOCS = path.join(ROOT, "architecture");
const OUT = path.join(ROOT, "index.html");

// Порядок групп в меню и в документе.
const LEVELS = [
  { level: "L1", dir: "l1", badge: "l1", title: "Контекст системы" },
  { level: "L2", dir: "l2", badge: "l2", title: "Процессы взаимодействия" },
  { level: "L3", dir: "l3", badge: "l3", title: "Технический справочник" },
  { level: "OPS", dir: "ops", badge: "ops", title: "Эксплуатация" },
  { level: "ADR", dir: "adr", badge: "adr", title: "Архитектурные решения" },
];

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const md = new MarkdownIt({ html: true, linkify: false, typographer: false });
md.use(attrs, { allowedAttributes: ["id", "class"] });
md.use(container, "tldr", {
  render: (tokens, i) => (tokens[i].nesting === 1 ? '<blockquote class="tldr">\n' : "</blockquote>\n"),
});
// Фигурные скобки в путях API оставляем как есть: api.html#tag/…/{id}.
const normalizeLink = md.normalizeLink;
md.normalizeLink = (url) => normalizeLink(url).replace(/%7B/g, "{").replace(/%7D/g, "}");
// Ссылки на API-справочник открываются в новой вкладке.
const linkOpen = md.renderer.rules.link_open || ((t, i, o, e, self) => self.renderToken(t, i, o));
md.renderer.rules.link_open = (tokens, i, opts, env, self) => {
  const t = tokens[i];
  if ((t.attrGet("href") || "").startsWith("api.html#")) {
    t.attrJoin("class", "api-ref");
    t.attrSet("target", "_blank");
    t.attrSet("rel", "noopener");
  }
  return linkOpen(tokens, i, opts, env, self);
};
const fence = md.renderer.rules.fence;
md.renderer.rules.fence = (tokens, i, opts, env, self) => {
  const t = tokens[i];
  if (t.info.trim() === "mermaid") return `<pre class="mermaid">\n${esc(t.content)}</pre>\n`;
  return fence(tokens, i, opts, env, self);
};

function readLevel(dir) {
  const abs = path.join(DOCS, dir);
  if (!fs.existsSync(abs)) return [];
  return fs
    .readdirSync(abs)
    .filter((f) => f.endsWith(".md"))
    .sort()
    .map((f) => {
      const file = path.join(abs, f);
      const { data, content } = matter(fs.readFileSync(file, "utf8"));
      for (const k of ["id", "level"]) {
        if (!data[k]) throw new Error(`${path.relative(ROOT, file)}: во front matter нет "${k}"`);
      }
      return { file, data, content };
    });
}

function renderSection({ data, content }) {
  const extra = Object.entries(data.attrs || {})
    .map(([k, v]) => ` ${k}="${esc(v)}"`)
    .join("");
  const body = md.render(content);
  const eyebrow = data.eyebrow != null ? `<div class="eyebrow">${data.eyebrow}</div>\n` : "";
  return `<section id="${esc(data.id)}" data-level="${esc(data.level)}"${extra}>\n${eyebrow}${body}</section>`;
}

function renderNav(groups) {
  return groups
    .filter((g) => g.pages.length)
    .map((g) => {
      const items = [];
      let group = null;
      for (const { data } of g.pages) {
        if (!data.nav) continue;
        if (data.group && data.group !== group) {
          group = data.group;
          items.push(`<li class="ns" role="presentation">${esc(group)}</li>`);
        }
        if (data.navSepBefore) items.push('<li class="nsep" role="separator"></li>');
        items.push(`<li><a href="#${esc(data.id)}">${esc(data.nav)}</a></li>`);
        for (const extra of [].concat(data.navAfter || [])) items.push(extra);
      }
      return [
        '<div class="ng">',
        '<div class="nh">',
        `<span class="nh-badge ${g.badge}">${g.level}</span>`,
        `<span class="nh-title">${esc(g.title)}</span>`,
        "</div>",
        "<ul>",
        ...items,
        "</ul>",
        "</div>",
      ].join("\n");
    })
    .join("\n\n");
}

const groups = LEVELS.map((l) => ({ ...l, pages: readLevel(l.dir) }));
const pages = groups.flatMap((g) => g.pages);
const seen = new Map();
for (const p of pages) {
  if (seen.has(p.data.id)) {
    throw new Error(`id "${p.data.id}" повторяется: ${seen.get(p.data.id)} и ${path.relative(ROOT, p.file)}`);
  }
  seen.set(p.data.id, path.relative(ROOT, p.file));
}

const shell = fs.readFileSync(path.join(DOCS, "_shell.html"), "utf8");
const aliases = fs.readFileSync(path.join(DOCS, "aliases.json"), "utf8").trim();
const notice = "<!-- Сгенерировано из architecture/ командой `npm run build`. Не редактируйте этот файл вручную. -->\n";
const out = shell
  .replace("{{nav}}", () => renderNav(groups))
  .replace("{{sections}}", () => pages.map(renderSection).join("\n\n"))
  .replace("{{aliases}}", () => aliases)
  .replace(/^(<!doctype html>\n?)/i, (m) => m + notice);

fs.writeFileSync(OUT, out);
console.log(`build: ${pages.length} разделов → ${path.relative(ROOT, OUT)}`);
