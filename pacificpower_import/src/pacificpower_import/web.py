"""Ingress web server — exposes scraper logs and debug captures."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEBUG_DIR = DATA_DIR / "debug"
LOG_FILE = DATA_DIR / "logs" / "main.log"
PORT = 8099

_SAFE_FILENAME = re.compile(r"^[\w\-\.]+$")


def _ingress_prefix(request: web.Request) -> str:
    return request.headers.get("X-Ingress-Path", "")


def _safe_name(name: str) -> bool:
    return bool(_SAFE_FILENAME.match(name)) and ".." not in name


def _tail_lines(path: Path, n: int) -> str:
    if not path.exists():
        return f"(log file not found: {path})"
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _debug_entries() -> list[dict]:
    """Return reverse-chronological list of debug capture pairs."""
    if not DEBUG_DIR.exists():
        return []
    pngs = {p.stem: p for p in DEBUG_DIR.glob("*.png")}
    htmls = {p.stem: p for p in DEBUG_DIR.glob("*.html")}
    stems = sorted(set(pngs) | set(htmls), reverse=True)
    entries = []
    for stem in stems:
        entries.append({
            "stem": stem,
            "has_png": stem in pngs,
            "has_html": stem in htmls,
        })
    return entries


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>Pacific Power - Diagnostics</title>
<style>
body {{
  font-family: monospace;
  background: #1a1a1a;
  color: #d0d0d0;
  margin: 0;
  padding: 1em;
}}
h2 {{
  color: #f0a040;
  margin-top: 1.5em;
  margin-bottom: 0.4em;
}}
pre {{
  background: #111;
  border: 1px solid #333;
  padding: 0.8em;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 60vh;
  overflow-y: auto;
}}
table {{
  border-collapse: collapse;
  width: 100%;
}}
th, td {{
  text-align: left;
  padding: 0.4em 0.8em;
  border-bottom: 1px solid #333;
}}
th {{
  color: #f0a040;
}}
img.thumb {{
  max-width: 240px;
  max-height: 150px;
  border: 1px solid #555;
  display: block;
}}
a {{
  color: #6af;
}}
.none {{
  color: #666;
  font-style: italic;
}}
</style>
</head>
<body>
<h1>Pacific Power Diagnostics</h1>
<p style="color:#888">Page auto-refreshes every 15 seconds.</p>

<h2>Recent logs (last 500 lines)</h2>
<pre id="log">{log_content}</pre>

<h2>Debug captures</h2>
{debug_table}

</body>
</html>
"""


def _render_debug_table(entries: list[dict], prefix: str) -> str:
    if not entries:
        return '<p class="none">No debug captures yet.</p>'
    rows = ["<table><tr><th>Name</th><th>Screenshot</th><th>HTML dump</th></tr>"]
    for e in entries:
        stem = e["stem"]
        if e["has_png"]:
            thumb = (
                f'<a href="{prefix}/images/{stem}.png">'
                f'<img class="thumb" src="{prefix}/images/{stem}.png" alt="{stem}">'
                f"</a>"
            )
        else:
            thumb = '<span class="none">—</span>'
        if e["has_html"]:
            html_link = f'<a href="{prefix}/html/{stem}.html">view HTML</a>'
        else:
            html_link = '<span class="none">—</span>'
        rows.append(f"<tr><td>{stem}</td><td>{thumb}</td><td>{html_link}</td></tr>")
    rows.append("</table>")
    return "\n".join(rows)


_DISABLED_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Pacific Power - Diagnostics disabled</title>
<style>
body {font-family: sans-serif; background:#1a1a1a; color:#d0d0d0; padding:2em; max-width:640px;}
h1 {color:#f0a040;} code {background:#111; padding:0.1em 0.4em; border-radius:3px;}
</style></head>
<body>
<h1>Diagnostics are disabled</h1>
<p>To capture screenshots + HTML dumps on scraper failures and view them here,
enable diagnostics in the add-on:</p>
<ol>
<li>Open the add-on <b>Configuration</b> tab.</li>
<li>Toggle <b>Diagnostics enabled</b> on.</li>
<li>Save. The add-on will restart automatically.</li>
</ol>
<p>While disabled, the scraper does not write any debug files and this page
shows no logs or captures.</p>
</body></html>
"""


def _diagnostics_enabled() -> bool:
    return os.environ.get("PP_DIAGNOSTICS_ENABLED", "false").lower() == "true"


async def handle_index(request: web.Request) -> web.Response:
    if not _diagnostics_enabled():
        return web.Response(text=_DISABLED_TEMPLATE, content_type="text/html")
    prefix = _ingress_prefix(request)
    log_content = _tail_lines(LOG_FILE, 500)
    entries = _debug_entries()
    debug_table = _render_debug_table(entries, prefix)
    body = _HTML_TEMPLATE.format(
        log_content=log_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        debug_table=debug_table,
    )
    return web.Response(text=body, content_type="text/html")


async def handle_logs(request: web.Request) -> web.Response:
    if not _diagnostics_enabled():
        raise web.HTTPForbidden(reason="diagnostics disabled")
    try:
        tail = int(request.rel_url.query.get("tail", "500"))
    except ValueError:
        tail = 500
    tail = min(max(tail, 1), 10000)
    content = _tail_lines(LOG_FILE, tail)
    return web.Response(text=content, content_type="text/plain")


async def handle_image(request: web.Request) -> web.Response:
    if not _diagnostics_enabled():
        raise web.HTTPForbidden(reason="diagnostics disabled")
    name = request.match_info["filename"]
    if not _safe_name(name) or not name.endswith(".png"):
        raise web.HTTPNotFound()
    path = DEBUG_DIR / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.Response(body=path.read_bytes(), content_type="image/png")


async def handle_html(request: web.Request) -> web.Response:
    if not _diagnostics_enabled():
        raise web.HTTPForbidden(reason="diagnostics disabled")
    name = request.match_info["filename"]
    if not _safe_name(name) or not name.endswith(".html"):
        raise web.HTTPNotFound()
    path = DEBUG_DIR / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.Response(
        text=path.read_text(errors="replace"),
        content_type="text/plain",
        charset="utf-8",
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/logs", handle_logs)
    app.router.add_get("/images/{filename}", handle_image)
    app.router.add_get("/html/{filename}", handle_html)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log.info("ingress web server starting on 0.0.0.0:%d", PORT)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=log)


if __name__ == "__main__":
    main()
