from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import asyncpg

from src.dashboard.snapshot import fetch_position_snapshot, snapshot_to_json_bytes

logger = logging.getLogger(__name__)

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>NXFH01 Positions</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 1rem 1.5rem; background: #0f1419; color: #e6edf3; }
    h1 { font-size: 1.25rem; }
    h2 { font-size: 1rem; margin-top: 1.5rem; color: #8b949e; }
    table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
    th, td { border: 1px solid #30363d; padding: 0.35rem 0.5rem; text-align: left; }
    th { background: #161b22; }
    tr:nth-child(even) { background: #161b22; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .open { color: #3fb950; }
    .closed { color: #79c0ff; }
    .neg { color: #f85149; }
    .pos { color: #3fb950; }
    #meta { color: #8b949e; font-size: 0.8rem; margin-bottom: 1rem; }
    button { margin-right: 0.5rem; padding: 0.35rem 0.75rem; cursor: pointer; }
  </style>
</head>
<body>
  <h1>NXFH01 — positions</h1>
  <p id="meta">Loading…</p>
  <button type="button" onclick="load()">Refresh</button>
  <h2>Open</h2>
  <div id="open-wrap"></div>
  <h2>Recent closed</h2>
  <div id="closed-wrap"></div>
  <script>
    function fmtUsd(n) {
      if (n == null || n === "") return "—";
      const x = Number(n);
      if (Number.isNaN(x)) return "—";
      return x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function fmtPct(n) {
      if (n == null || n === "") return "—";
      const x = Number(n) * 100;
      if (Number.isNaN(x)) return "—";
      return x.toFixed(3) + "%";
    }
    function pnlClass(n) {
      if (n == null || n === "") return "";
      const x = Number(n);
      if (x > 0) return "pos";
      if (x < 0) return "neg";
      return "";
    }
    function tableOpen(rows) {
      if (!rows.length) return "<p>No open rows in journal (outcome_recorded_at IS NULL).</p>";
      let h = "<table><thead><tr>";
      h += "<th>Status</th><th>Engine</th><th>Strategy</th><th>Coin</th><th>Side</th>";
      h += "<th class='num'>$ notional</th><th class='num'>Lev</th><th class='num'>Entry</th><th>Opened</th><th>Source</th></tr></thead><tbody>";
      for (const r of rows) {
        h += "<tr>";
        h += "<td class='open'>" + r.status + "</td>";
        h += "<td>" + r.engine_id + "</td>";
        h += "<td>" + r.strategy_key + "</td>";
        h += "<td>" + r.coin + "</td>";
        h += "<td>" + r.side + "</td>";
        h += "<td class='num'>" + fmtUsd(r.notional_usd) + "</td>";
        h += "<td class='num'>" + (r.leverage ?? "—") + "</td>";
        h += "<td class='num'>" + fmtUsd(r.entry_price) + "</td>";
        h += "<td>" + (r.opened_at || "—") + "</td>";
        h += "<td>" + r.source + "</td>";
        h += "</tr>";
      }
      h += "</tbody></table>";
      return h;
    }
    function tableClosed(rows) {
      if (!rows.length) return "<p>No closed rows yet.</p>";
      let h = "<table><thead><tr>";
      h += "<th>Status</th><th>Engine</th><th>Coin</th><th>Side</th>";
      h += "<th class='num'>$ notional</th><th class='num'>Lev</th><th class='num'>PnL $</th><th class='num'>PnL %</th>";
      h += "<th>Exit</th><th>Closed</th><th>Source</th></tr></thead><tbody>";
      for (const r of rows) {
        h += "<tr>";
        h += "<td class='closed'>" + r.status + "</td>";
        h += "<td>" + r.engine_id + "</td>";
        h += "<td>" + r.coin + "</td>";
        h += "<td>" + r.side + "</td>";
        h += "<td class='num'>" + fmtUsd(r.notional_usd) + "</td>";
        h += "<td class='num'>" + (r.leverage ?? "—") + "</td>";
        h += "<td class='num " + pnlClass(r.pnl_usd) + "'>" + fmtUsd(r.pnl_usd) + "</td>";
        h += "<td class='num " + pnlClass(r.pnl_pct) + "'>" + fmtPct(r.pnl_pct) + "</td>";
        h += "<td>" + (r.exit_reason || "—") + "</td>";
        h += "<td>" + (r.closed_at || "—") + "</td>";
        h += "<td>" + r.source + "</td>";
        h += "</tr>";
      }
      h += "</tbody></table>";
      return h;
    }
    async function load() {
      document.getElementById("meta").textContent = "Loading…";
      try {
        const res = await fetch("/api/snapshot");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);
        const s = data.summary || {};
        document.getElementById("meta").textContent =
          "Open: " + s.open_count + " (" + fmtUsd(s.open_notional_usd) + " notional) · " +
          "Closed rows shown: " + s.closed_shown + " · " +
          "Sum PnL (first 50 closed): " + fmtUsd(s.closed_pnl_sum_usd_recent);
        document.getElementById("open-wrap").innerHTML = tableOpen(data.open || []);
        document.getElementById("closed-wrap").innerHTML = tableClosed(data.closed || []);
      } catch (e) {
        document.getElementById("meta").textContent = "Error: " + e;
      }
    }
    load();
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    dsn: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("DASHBOARD_HTTP " + fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "").split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = _HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            try:
                snap = asyncio.run(_snapshot_once(self.dsn))
                body = snapshot_to_json_bytes(snap)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                logger.exception("DASHBOARD_SNAPSHOT_FAILED error=%s", e)
                err = snapshot_to_json_bytes({"error": str(e), "open": [], "closed": [], "summary": {}})
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return
        self.send_error(404, "Not Found")


async def _snapshot_once(dsn: str) -> dict[str, Any]:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        return await fetch_position_snapshot(pool)
    finally:
        await pool.close()


class _DualStackLoopbackHTTPServer(ThreadingHTTPServer):
    """Bind ::1 with IPV6_V6ONLY=0 so ``localhost`` (often IPv6 on macOS) and 127.0.0.1 both reach the server."""

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket = socket.socket(self.address_family, self.socket_type)
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        try:
            if self.allow_reuse_address:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        self.socket.bind(self.server_address)
        sock_host, sock_port = self.socket.getsockname()[:2]
        self.server_name = str(sock_host)
        self.server_port = int(sock_port)


def _localhost_names() -> frozenset[str]:
    return frozenset({"127.0.0.1", "localhost", "::1"})


def run_server(*, host: str, port: int, dsn: str) -> None:
    _DashboardHandler.dsn = dsn
    host_norm = (host or "").strip().lower()
    if host_norm in _localhost_names():
        try:
            server = _DualStackLoopbackHTTPServer(("::1", port), _DashboardHandler)
            logger.info(
                "DASHBOARD_LISTEN mode=dual_stack_loopback port=%d "
                "urls=http://127.0.0.1:%d/ http://[::1]:%d/ "
                "(use 127.0.0.1 or [::1] if localhost fails)",
                port,
                port,
                port,
            )
        except OSError as e:
            logger.warning(
                "DASHBOARD_IPV6_BIND_FAILED error=%s fallback=127.0.0.1",
                e,
            )
            server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
            logger.info(
                "DASHBOARD_LISTEN host=127.0.0.1 port=%d url=http://127.0.0.1:%d/",
                port,
                port,
            )
    else:
        server = ThreadingHTTPServer((host, port), _DashboardHandler)
        logger.info(
            "DASHBOARD_LISTEN host=%s port=%d url=http://%s:%d/",
            host,
            port,
            host,
            port,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("DASHBOARD_SHUTDOWN reason=keyboard_interrupt")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(description="NXFH01 read-only positions dashboard (Postgres journal).")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default loopback). For 127.0.0.1/localhost, uses IPv6 ::1 dual-stack so "
        "http://localhost:PORT works on macOS; use 0.0.0.0 for LAN access.",
    )
    p.add_argument("--port", type=int, default=8765, help="TCP port (default 8765).")
    p.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres URL (default: env DATABASE_URL).",
    )
    args = p.parse_args(argv)
    if not args.database_url or not str(args.database_url).strip():
        raise SystemExit("DATABASE_URL missing: set env or pass --database-url")
    run_server(host=args.host, port=args.port, dsn=str(args.database_url).strip())


if __name__ == "__main__":
    main()
