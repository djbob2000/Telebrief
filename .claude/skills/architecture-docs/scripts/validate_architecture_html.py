#!/usr/bin/env python3
"""Lightweight structural and link validator for architecture documentation HTML."""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class DocParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.levels: list[str] = []
        self.mermaid_blocks = 0
        self.mermaid_sources: list[str] = []
        self._in_mermaid = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if value := attr.get("id"):
            self.ids.add(value)
        if tag == "a" and (href := attr.get("href")):
            self.hrefs.append(href)
        if tag == "section" and (level := attr.get("data-level")):
            self.levels.append(level.upper())
        if tag == "pre" and "mermaid" in set((attr.get("class") or "").split()):
            self.mermaid_blocks += 1
            self.mermaid_sources.append("")
            self._in_mermaid = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_mermaid = False

    def handle_data(self, data: str) -> None:
        if self._in_mermaid:
            self.mermaid_sources[-1] += data


def local_file_target(html_path: Path, href: str) -> Path | None:
    """Return a local file target for a relative href, otherwise None."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return None
    if href.startswith(("#", "/", "?")):
        return None
    if not parsed.path:
        return None
    return (html_path.parent / unquote(parsed.path)).resolve()


def validate(path: Path) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    parser = DocParser()
    parser.feed(text)

    errors: list[str] = []
    warnings: list[str] = []

    if not parser.ids:
        errors.append("No element IDs found; internal navigation cannot work.")

    missing_anchors = sorted(
        {h[1:] for h in parser.hrefs if h.startswith("#") and h[1:] not in parser.ids}
    )
    if missing_anchors:
        errors.append("Broken internal anchors: " + ", ".join(missing_anchors[:25]))

    missing_files: list[str] = []
    for href in parser.hrefs:
        target = local_file_target(path, href)
        if target is not None and not target.exists():
            missing_files.append(href)
    if missing_files:
        errors.append("Broken relative file links: " + ", ".join(sorted(set(missing_files))[:25]))

    levels = set(parser.levels)
    for required in ("L1", "L2", "L3"):
        if required not in levels:
            errors.append(f'Missing section data-level="{required}".')

    local_paths = sorted(set(re.findall(r"(?:file://)?/(?:Users|home)/[^\s\"'<>)]+", text)))
    if local_paths:
        errors.append("Local absolute paths (use repository-relative paths): " + ", ".join(local_paths[:10]))

    for i, block in enumerate(parser.mermaid_sources):
        body = block.strip()
        if body.startswith("sequenceDiagram"):
            bad = [ln.strip() for ln in body.splitlines()[1:] if ";" in ln and not ln.strip().startswith("%%")]
            if bad:
                errors.append(f"Mermaid block #{i}: ';' inside sequenceDiagram breaks rendering: {bad[0][:80]}")

    if parser.mermaid_blocks == 0:
        warnings.append("No Mermaid diagram blocks found.")

    if not re.search(r"TL;DR", text, flags=re.I):
        warnings.append("No TL;DR marker found.")

    if not re.search(r"①|&#9312;|&#x2460;|Step\s*1", text, flags=re.I):
        warnings.append("No obvious numbered interaction stage found for L2.")

    if "Needs verification" not in text and "TODO: verify with team" not in text:
        warnings.append("No verification marker appears; ensure all uncertain details were actually confirmed.")

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html", type=Path)
    args = ap.parse_args()

    if not args.html.is_file():
        print(f"ERROR: file not found: {args.html}", file=sys.stderr)
        return 2

    errors, warnings = validate(args.html)

    for item in errors:
        print(f"ERROR: {item}")
    for item in warnings:
        print(f"WARN: {item}")

    if errors:
        print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1

    print(f"OK: structural checks passed with {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
