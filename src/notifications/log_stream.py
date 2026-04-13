"""Log streaming — routes filtered agent logs to a web dashboard and Slack.

Uses only stdlib (http.server, logging, threading) + httpx (already a dep).
All servers run as daemon threads inside the main process.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

_RELEVANT_MODULES = (
    "src.execution",
    "src.risk",
    "src.strategy",
    "src.skill.functions",
    "src.market.liquidation_feed",
    "src.learning",
    "src.enrichment",
    "nxfh02",
)

_ALWAYS_FORWARD_LEVELS = (logging.WARNING, logging.ERROR, logging.CRITICAL)


class _LogEntry:
    __slots__ = ("ts", "level", "name", "message")

    def __init__(self, ts: float, level: str, name: str, message: str):
        self.ts = ts
        self.level = level
        self.name = name
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "level": self.level,
            "name": self.name,
            "message": self.message,
        }

    def to_text(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"[{t}] {self.level} {self.name}: {self.message}"


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------

class RingBuffer:
    def __init__(self, maxlen: int = 500):
        self._buf: deque[_LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, entry: _LogEntry) -> None:
        with self._lock:
            self._buf.append(entry)

    def since(self, ts: float) -> list[_LogEntry]:
        with self._lock:
            return [e for e in self._buf if e.ts > ts]

    def all(self) -> list[_LogEntry]:
        with self._lock:
            return list(self._buf)


# ---------------------------------------------------------------------------
# Slack poster — batches messages every N seconds
# ---------------------------------------------------------------------------

class SlackPoster:
    def __init__(self, webhook_url: str, batch_interval: float = 5.0):
        self._url = webhook_url
        self._interval = batch_interval
        self._queue: deque[_LogEntry] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=10)
        self._running = False
        self._thread: threading.Thread | None = None

    def enqueue(self, entry: _LogEntry) -> None:
        with self._lock:
            self._queue.append(entry)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="slack-poster")
        self._thread.start()
        logger.info("Slack log poster started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._interval)
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            if not self._queue:
                return
            batch = list(self._queue)
            self._queue.clear()

        lines = [e.to_text() for e in batch]
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3800] + "\n... (truncated)"

        try:
            resp = self._client.post(self._url, json={"text": f"```\n{text}\n```"})
            if resp.status_code != 200:
                logger.debug("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("Slack post failed: %s", e)


# ---------------------------------------------------------------------------
# Logging handler — filters + fans out to buffer and Slack
# ---------------------------------------------------------------------------

class LogStreamHandler(logging.Handler):
    def __init__(self, buffer: RingBuffer, slack: SlackPoster | None = None):
        super().__init__(level=logging.DEBUG)
        self._buffer = buffer
        self._slack = slack

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.name.startswith(_RELEVANT_MODULES)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = _LogEntry(
                ts=record.created,
                level=record.levelname,
                name=record.name,
                message=self.format(record),
            )
            self._buffer.append(entry)
            if self._slack:
                self._slack.enqueue(entry)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>NXFH02 Logs</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0d1117;color:#c9d1d9;font-family:'JetBrains Mono',Consolas,monospace;font-size:13px}
  #header{position:sticky;top:0;background:#161b22;border-bottom:1px solid #30363d;padding:10px 16px;display:flex;align-items:center;gap:12px;z-index:10}
  #header h1{font-size:16px;font-weight:600;color:#58a6ff}
  #status{font-size:12px;color:#8b949e}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:4px}
  #controls{margin-left:auto;display:flex;gap:8px}
  #controls button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px}
  #controls button:hover{border-color:#58a6ff}
  #controls button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
  #log{padding:8px 16px;overflow-y:auto;height:calc(100vh - 52px)}
  .entry{padding:2px 0;white-space:pre-wrap;word-break:break-all;line-height:1.5}
  .ts{color:#8b949e}
  .lvl-INFO{color:#58a6ff}
  .lvl-WARNING{color:#d29922}
  .lvl-ERROR{color:#f85149}
  .lvl-CRITICAL{color:#f85149;font-weight:bold}
  .mod{color:#7ee787}
  .msg-trade{color:#3fb950}
</style>
</head>
<body>
<div id="header">
  <h1>NXFH02 Logs</h1>
  <span id="status"><span class="dot"></span>Live</span>
  <div id="controls">
    <button id="btnPause">Pause</button>
    <button id="btnScroll" class="active">Auto-scroll</button>
  </div>
</div>
<div id="log"></div>
<script>
let lastTs = 0, paused = false, autoScroll = true;
const log = document.getElementById('log');

document.getElementById('btnPause').addEventListener('click', function() {
  paused = !paused;
  this.textContent = paused ? 'Resume' : 'Pause';
  this.classList.toggle('active', paused);
});
document.getElementById('btnScroll').addEventListener('click', function() {
  autoScroll = !autoScroll;
  this.classList.toggle('active', autoScroll);
});

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderEntry(e) {
  const t = new Date(e.ts * 1000).toLocaleTimeString();
  const isTrade = e.name.startsWith('src.execution');
  const div = document.createElement('div');
  div.className = 'entry';
  div.innerHTML =
    '<span class="ts">' + t + '</span> ' +
    '<span class="lvl-' + e.level + '">' + e.level.padEnd(8) + '</span> ' +
    '<span class="mod">' + escHtml(e.name) + '</span>: ' +
    '<span class="' + (isTrade ? 'msg-trade' : '') + '">' + escHtml(e.message) + '</span>';
  return div;
}

async function poll() {
  if (paused) return;
  try {
    const r = await fetch('/api/logs?since=' + lastTs);
    const entries = await r.json();
    if (entries.length > 0) {
      const frag = document.createDocumentFragment();
      entries.forEach(e => {
        frag.appendChild(renderEntry(e));
        if (e.ts > lastTs) lastTs = e.ts;
      });
      log.appendChild(frag);
      if (autoScroll) log.scrollTop = log.scrollHeight;
    }
    document.querySelector('.dot').style.background = '#3fb950';
  } catch {
    document.querySelector('.dot').style.background = '#f85149';
  }
}
setInterval(poll, 3000);
poll();
</script>
</body>
</html>
"""


class _LogRequestHandler(BaseHTTPRequestHandler):
    buffer: RingBuffer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            self._serve_html()
        elif parsed.path == "/api/logs":
            self._serve_json(parsed.query)
        else:
            self.send_error(404)

    def _serve_html(self) -> None:
        body = _DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, query: str) -> None:
        params = parse_qs(query)
        since = float(params.get("since", ["0"])[0])
        entries = self.buffer.since(since)
        body = json.dumps([e.to_dict() for e in entries]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class LogWebServer:
    def __init__(self, buffer: RingBuffer, port: int = 8877):
        handler = type("Handler", (_LogRequestHandler,), {"buffer": buffer})
        self._server = HTTPServer(("0.0.0.0", port), handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="log-web",
        )
        self._thread.start()
        logger.info("Log dashboard listening on http://0.0.0.0:%d", self._server.server_port)

    def stop(self) -> None:
        self._server.shutdown()


# ---------------------------------------------------------------------------
# Public setup
# ---------------------------------------------------------------------------

def setup_log_stream(
    slack_webhook_url: str = "",
    web_port: int = 8877,
) -> LogWebServer | None:
    """Attach the log stream handler to the root logger.

    Returns the web server instance (for shutdown), or None if nothing was enabled.
    """
    has_slack = bool(slack_webhook_url)
    has_web = web_port > 0

    if not has_slack and not has_web:
        return None

    buf = RingBuffer(maxlen=500)

    slack: SlackPoster | None = None
    if has_slack:
        slack = SlackPoster(slack_webhook_url, batch_interval=5.0)
        slack.start()

    handler = LogStreamHandler(buf, slack)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)

    web: LogWebServer | None = None
    if has_web:
        try:
            web = LogWebServer(buf, web_port)
            web.start()
        except OSError as e:
            logger.warning("Log web server failed to start on port %d: %s", web_port, e)

    sources = []
    if has_slack:
        sources.append("Slack")
    if web:
        sources.append(f"web :{ web_port}")
    logger.info("Log streaming active → %s", " + ".join(sources))

    return web
