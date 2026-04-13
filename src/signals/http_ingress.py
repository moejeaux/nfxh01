"""HTTP webhook → bounded queue → worker → OrderExecutor.execute_signal (DegenClaw)."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.signals.auth import WebhookAuthVerifier
from src.signals.intent import SignalIntent
from src.signals.map_intent import signal_intent_to_strategy_signal_with_equity
from src.state.persistence import StateStore

if TYPE_CHECKING:
    from src.skill.functions import SkillContext

log = logging.getLogger("nxfh02.signal")


def _headers_to_dict(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {k.lower(): v for k, v in handler.headers.items()}


def _send_json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    """HTTP/1.1 requires Content-Length (or chunked/close) so clients know when the body ends."""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    for hk, hv in extra_headers or ():
        handler.send_header(hk, hv)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _normalized_signal_path(handler: BaseHTTPRequestHandler) -> str:
    """Path only (no query string); BaseHTTPRequestHandler.path may include ?…"""
    path_only = urlparse(handler.path).path or "/"
    return path_only.rstrip("/") or "/"


class HttpSignalIngress:
    """POST /v1/signal — auth → validate → allowlist → idempotency → queue → executor."""

    def __init__(
        self,
        ctx: SkillContext,
        store: StateStore,
        verifier: WebhookAuthVerifier,
        allowed_symbols: set[str],
        queue_max: int = 32,
    ) -> None:
        self._ctx = ctx
        self._store = store
        self._verifier = verifier
        self._allowed = {s.upper() for s in allowed_symbols}
        self._q: queue.Queue[tuple[SignalIntent, int]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        import os

        host = (os.getenv("NXFH02_SIGNAL_HTTP_HOST") or "127.0.0.1").strip()
        port = int((os.getenv("NXFH02_SIGNAL_HTTP_PORT") or "8788").strip())
        self._bind = (host, port)

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run_worker, name="signal-worker", daemon=True)
        self._worker.start()

        handler_cls = self._make_handler()

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True

        self._server = Server(self._bind, handler_cls)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="signal-http",
            daemon=True,
        )
        self._server_thread.start()
        log.info(
            "SIGNAL_HTTP_LISTENING bind=%s:%s queue_max=%s",
            self._bind[0],
            self._bind[1],
            self._q.maxsize,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        if self._worker:
            self._worker.join(timeout=5)
        if self._server_thread:
            self._server_thread.join(timeout=2)

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                intent, audit_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process_one(intent, audit_id)
            self._q.task_done()

    def _process_one(self, intent: SignalIntent, audit_id: int) -> None:
        sig = intent.signal_id[:24]
        try:
            equity = self._ctx.risk.state.equity
            signal = signal_intent_to_strategy_signal_with_equity(
                intent, self._ctx.config, equity
            )
            log.info(
                "SIGNAL_NORMALIZED signal_id=%s origin=senpi symbol=%s side=%s",
                sig,
                signal.coin,
                signal.side,
            )
            log.info(
                "SIGNAL_ROUTE_DEGEN signal_id=%s backend=degen_claw (NXFH02_SIGNAL_SOURCE=senpi)",
                sig,
            )
            mids = self._ctx.feed.refresh_prices()
            mid = mids.get(signal.coin)
            if not mid:
                log.warning("SIGNAL_EXEC_FAIL signal_id=%s reason=no_mid_for_symbol", sig)
                self._store.update_ingress_audit(audit_id, "failed", "no_mid_for_symbol")
                self._store.update_signal_id_status(intent.signal_id, "failed_no_mid")
                return

            funding = self._ctx.feed.get_funding_rate(signal.coin)
            eff = self._ctx.effective_min_signal_confidence
            log.info("SIGNAL_EXECUTE_BEGIN signal_id=%s origin=senpi", sig)
            from src.notifications.trade_candidate_intel import (
                log_and_notify_trade_candidate_pre_execution,
            )

            thesis_txt = (intent.thesis or "").strip() or intent.signal_id
            log_and_notify_trade_candidate_pre_execution(
                self._ctx,
                symbol=signal.coin,
                direction=signal.side,
                thesis=thesis_txt,
                headline="Senpi HTTP signal candidate (pre-execution)",
                signal=signal,
                mid=mid,
                funding_rate=funding,
            )
            result = self._ctx.executor.execute_signal(
                signal,
                mid,
                funding,
                effective_min_confidence=eff,
                skip_smart_money_enrichment=True,
            )
            if result.executed:
                log.info(
                    "SIGNAL_SUBMIT_OK signal_id=%s origin=senpi job_id=%s",
                    sig,
                    result.job_id,
                )
                self._store.update_ingress_audit(
                    audit_id, "executed", str(result.job_id or "")
                )
                self._store.update_signal_id_status(intent.signal_id, "executed")
            else:
                log.warning(
                    "SIGNAL_RISK_OR_SUBMIT_FAIL signal_id=%s origin=senpi reason=%s",
                    sig,
                    result.reason,
                )
                self._store.update_ingress_audit(
                    audit_id, "rejected", (result.reason or "")[:500]
                )
                self._store.update_signal_id_status(intent.signal_id, "rejected")
        except Exception as e:
            log.exception("SIGNAL_WORKER_ERROR signal_id=%s err=%s", sig, e)
            self._store.update_ingress_audit(audit_id, "error", str(e)[:500])
            self._store.update_signal_id_status(intent.signal_id, "error")

    def _make_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug(fmt, *args)

            def do_GET(self) -> None:  # noqa: N802
                norm_path = _normalized_signal_path(self)
                if norm_path == "/health":
                    body = json.dumps({
                        "status": "ok",
                        "queue_depth": parent._q.qsize(),
                        "queue_max": parent._q.maxsize,
                        "allowed_symbols": sorted(parent._allowed),
                    }).encode("utf-8")
                    _send_json_response(self, 200, body)
                    return
                self.send_error(404, "not found")

            def do_POST(self) -> None:  # noqa: N802
                norm_path = _normalized_signal_path(self)
                if norm_path not in ("/v1/signal", "/signal"):
                    self.send_error(404, "not found")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                hmap = _headers_to_dict(self)

                if not parent._verifier.verify(body, hmap):
                    log.warning("SIGNAL_HTTP_UNAUTHORIZED path=%s", self.path)
                    _send_json_response(self, 401, b'{"error":"unauthorized"}')
                    return

                log.info("SIGNAL_HTTP_RECEIVED path=%s bytes=%d", self.path, len(body))

                try:
                    data = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    log.warning("SIGNAL_HTTP_BAD_JSON err=%s", e)
                    _send_json_response(self, 400, b'{"error":"invalid_json"}')
                    return

                try:
                    intent = SignalIntent.model_validate(data)
                except Exception as e:
                    log.warning("SIGNAL_HTTP_VALIDATE_FAIL err=%s", e)
                    _send_json_response(
                        self,
                        400,
                        json.dumps({"error": "validation_failed", "detail": str(e)}).encode(),
                    )
                    return

                sym = intent.symbol.strip().upper()
                if sym not in parent._allowed:
                    log.warning(
                        "SIGNAL_HTTP_ALLOWLIST_REJECT signal_id=%s symbol=%s",
                        intent.signal_id[:24],
                        sym,
                    )
                    _send_json_response(
                        self,
                        400,
                        json.dumps({"error": "symbol_not_allowed", "symbol": sym}).encode(),
                    )
                    return

                try:
                    intent, tif_notes = intent.resolve_execution_constraints()
                except ValueError as e:
                    log.warning(
                        "SIGNAL_TIF_REJECT signal_id=%s err=%s",
                        intent.signal_id[:24],
                        e,
                    )
                    _send_json_response(
                        self,
                        400,
                        json.dumps({"error": "execution_constraint", "detail": str(e)}).encode(),
                    )
                    return

                if tif_notes:
                    log.info(
                        "SIGNAL_TIF_NOTES signal_id=%s notes=%s",
                        intent.signal_id[:24],
                        tif_notes,
                    )

                if not parent._store.try_claim_signal_id(intent.signal_id, "queued"):
                    log.warning(
                        "SIGNAL_DUPLICATE_REJECTED signal_id=%s",
                        intent.signal_id[:24],
                    )
                    _send_json_response(
                        self,
                        202,
                        json.dumps(
                            {
                                "status": "duplicate",
                                "signal_id": intent.signal_id,
                                "accepted": False,
                            }
                        ).encode(),
                    )
                    return

                body_hash = hashlib.sha256(body).hexdigest()[:32]
                audit_id = parent._store.insert_ingress_audit(
                    intent.signal_id,
                    body_hash,
                    "accepted_queued",
                    "",
                )

                try:
                    parent._q.put_nowait((intent, audit_id))
                except queue.Full:
                    log.error(
                        "SIGNAL_QUEUE_SATURATED signal_id=%s queue_max=%s",
                        intent.signal_id[:24],
                        parent._q.maxsize,
                    )
                    parent._store.release_signal_id(intent.signal_id)
                    parent._store.update_ingress_audit(
                        audit_id, "queue_saturated", "queue_full"
                    )
                    _send_json_response(
                        self,
                        503,
                        json.dumps(
                            {
                                "error": "queue_saturated",
                                "signal_id": intent.signal_id,
                            }
                        ).encode(),
                        extra_headers=[("Retry-After", "5")],
                    )
                    return

                log.info(
                    "SIGNAL_HTTP_QUEUED signal_id=%s audit_id=%s queue_depth≈%s",
                    intent.signal_id[:24],
                    audit_id,
                    parent._q.qsize(),
                )
                _send_json_response(
                    self,
                    202,
                    json.dumps(
                        {
                            "status": "accepted",
                            "signal_id": intent.signal_id,
                            "request_id": intent.signal_id,
                            "accepted": True,
                            "audit_id": audit_id,
                        }
                    ).encode(),
                )

        return Handler
