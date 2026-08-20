"""Ingress web server — exposes scraper logs and debug captures."""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from . import progress, status

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
<p class="hint">Reload the page to update.</p>

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
    from .runtime_flags import diagnostics_enabled
    return diagnostics_enabled()


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


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"~{seconds}s"
    if seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"~{m}m {s}s"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"~{h}h {m}m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"~{d}d {h}h"


_STATUS_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pacific Power - Status</title>
<style>
body {{
  font-family: monospace;
  background: #1a1a1a;
  color: #d0d0d0;
  margin: 0;
  padding: 1em;
}}
nav {{
  margin-bottom: 1.5em;
}}
nav a {{
  color: #6af;
  margin-right: 1.5em;
  text-decoration: none;
}}
nav a.active {{
  color: #f0a040;
  font-weight: bold;
}}
h1 {{
  color: #f0a040;
  margin-bottom: 0.3em;
}}
h2 {{
  color: #f0a040;
  margin-top: 1.5em;
  margin-bottom: 0.4em;
}}
.stat-label {{
  color: #888;
  display: inline-block;
  width: 14em;
}}
.error {{
  color: #f66;
}}
.hint {{
  color: #888;
}}
progress {{
  width: 100%;
  height: 1.2em;
  margin: 0.5em 0;
}}
.badge {{
  display: inline-block;
  padding: 0.2em 0.7em;
  border-radius: 3px;
  font-weight: bold;
  font-size: 0.95em;
}}
.badge-up-to-date {{ background: #2a6a2a; color: #8fea8f; }}
.badge-backfilling {{ background: #6a4a00; color: #f0c060; }}
.badge-error {{ background: #6a1a1a; color: #f08080; }}
.summary-block {{
  border: 1px solid #333;
  background: #111;
  padding: 0.8em 1em;
  margin-bottom: 1.5em;
}}
.summary-block p {{
  margin: 0.3em 0;
}}
</style>
</head>
<body>
<nav>
  <a href="{prefix}/" class="active">Status</a>
  <a href="{prefix}/diagnostics">Diagnostics</a>
</nav>
<h1>Pacific Power - Status</h1>
<p class="hint">Reload the page to update.</p>

{summary_block}

<h2>Task</h2>
<p><span class="stat-label">Task:</span> {task}</p>
<p><span class="stat-label">Progress:</span> {done} / {total} ({pct}%)</p>
<progress value="{done}" max="{total}"></progress>
<p><span class="stat-label">Current item:</span> {current_label}</p>
<p><span class="stat-label">Avg seconds/item:</span> {ewma}</p>
<p><span class="stat-label">ETA:</span> {eta_str}</p>
<p><span class="stat-label">Finishes at (UTC):</span> {finishes_at}</p>

<h2>Timestamps</h2>
<p><span class="stat-label">Started at:</span> {started_at}</p>
<p><span class="stat-label">Updated at:</span> {updated_at}</p>

{error_section}
</body>
</html>
"""

_ERROR_SECTION = """\
<h2>Last error</h2>
<p class="error">{last_error}</p>
<p><span class="stat-label">Error at:</span> {last_error_at}</p>
"""

_SUMMARY_ERROR_ROW = """\
<p><span class="stat-label">Last error:</span> <span class="error">{last_error}</span></p>
<p><span class="stat-label">Error at:</span> {last_error_at}</p>
"""

_SUMMARY_BLOCK = """\
<div class="summary-block">
<p><span class="badge badge-{state}">{state}</span></p>
<p><span class="stat-label">Newest data:</span> {newest_data_date}</p>
<p><span class="stat-label">Last run finished:</span> {last_run_finished}</p>
<p><span class="stat-label">Next run:</span> {next_run_at}</p>
<p><span class="stat-label">Schedule:</span> {schedule_cron}</p>
{error_rows}</div>
"""


def _render_summary(snap: "status.StatusSnapshot", now: datetime) -> str:
    newest = snap.newest_data_date.isoformat() if snap.newest_data_date is not None else "—"
    last_finished = snap.last_run_finished_at.strftime("%Y-%m-%d %H:%M UTC") if snap.last_run_finished_at is not None else "—"
    next_run = snap.next_run_at.strftime("%Y-%m-%d %H:%M UTC") if snap.next_run_at is not None else "—"
    cron = html.escape(snap.schedule_cron) if snap.schedule_cron else "—"

    error_rows = ""
    if snap.last_error is not None and snap.last_error_at is not None:
        age = now - snap.last_error_at
        if age.total_seconds() < 7 * 86400:
            error_rows = _SUMMARY_ERROR_ROW.format(
                last_error=html.escape(snap.last_error),
                last_error_at=snap.last_error_at.strftime("%Y-%m-%d %H:%M UTC"),
            )

    return _SUMMARY_BLOCK.format(
        state=snap.state,
        newest_data_date=newest,
        last_run_finished=last_finished,
        next_run_at=next_run,
        schedule_cron=cron,
        error_rows=error_rows,
    )


async def handle_status(request: web.Request) -> web.Response:
    from .state import State

    prefix = _ingress_prefix(request)
    p = progress.load()
    now = datetime.now(timezone.utc)

    state_path = DATA_DIR / "state.json"
    state = State.load(state_path)
    last_run = status.load_last_run()
    cron_expr = os.environ.get("PP_DAILY_SCHEDULE") or None
    snap = status.compute(state, p, last_run, cron_expr=cron_expr, now=now)
    summary_block = _render_summary(snap, now)

    pct = int(p.done / p.total * 100) if p.total > 0 else 0
    ewma_str = f"{p.ewma_seconds_per_item:.1f}s" if p.ewma_seconds_per_item is not None else "—"
    eta = progress.eta_seconds(p)
    eta_str_val = _fmt_duration(eta)
    if eta is not None and eta_str_val != "—":
        eta_str = f"{eta_str_val} (rolling estimate)"
    else:
        eta_str = eta_str_val
    if eta is not None:
        finishes_at = (now + timedelta(seconds=eta)).strftime("%Y-%m-%d %H:%M UTC")
    else:
        finishes_at = "—"
    error_section = (
        _ERROR_SECTION.format(
            last_error=html.escape(str(p.last_error)),
            last_error_at=p.last_error_at or "—",
        )
        if p.last_error
        else ""
    )
    body = _STATUS_TEMPLATE.format(
        prefix=prefix,
        summary_block=summary_block,
        task=p.task,
        done=p.done,
        total=p.total,
        pct=pct,
        current_label=p.current_label or "—",
        ewma=ewma_str,
        eta_str=eta_str,
        finishes_at=finishes_at,
        started_at=p.started_at or "—",
        updated_at=p.updated_at or "—",
        error_section=error_section,
    )
    return web.Response(text=body, content_type="text/html")


async def handle_status_json(request: web.Request) -> web.Response:
    from .state import State

    now = datetime.now(timezone.utc)
    p = progress.load()
    state = State.load(DATA_DIR / "state.json")
    last_run = status.load_last_run()
    cron_expr = os.environ.get("PP_DAILY_SCHEDULE") or None
    snap = status.compute(state, p, last_run, cron_expr=cron_expr, now=now)

    def _snap_dict(s: "status.StatusSnapshot") -> dict:
        return {
            "state": s.state,
            "newest_data_date": s.newest_data_date.isoformat() if s.newest_data_date is not None else None,
            "last_run_started_at": s.last_run_started_at.isoformat() if s.last_run_started_at is not None else None,
            "last_run_finished_at": s.last_run_finished_at.isoformat() if s.last_run_finished_at is not None else None,
            "last_run_ok": s.last_run_ok,
            "last_error": s.last_error,
            "last_error_at": s.last_error_at.isoformat() if s.last_error_at is not None else None,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at is not None else None,
            "schedule_cron": s.schedule_cron,
        }

    return web.json_response({"summary": _snap_dict(snap), "progress": asdict(p)})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_status)
    app.router.add_get("/status.json", handle_status_json)
    app.router.add_get("/diagnostics", handle_index)
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
