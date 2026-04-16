"""ACP wrapper for trade submission via ACP v2 (SDK HTTP sidecar) or legacy CLI.

All trade execution goes through ACP jobs created by this project.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_WALLET = ""


def extract_acp_job_id(stdout: str) -> str | None:
    """
    Parse ACP CLI stdout for a numeric job id.

    Supports:
    - ``Job 1003224169 created`` (legacy)
    - ``Job Created`` blocks with ``Job ID             1003416033`` (newer CLI)
    """
    if not stdout or not stdout.strip():
        return None
    text = stdout.strip()
    patterns = (
        r"(?mi)^\s*Job\s+ID[:\s]+(\d+)\s*$",
        r"(?i)Job\s+ID[:\s]+(\d+)",
        r"Job\s+(\d+)\s+created",
        r"Job\s+(\d+)",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


# ── request/response models ─────────────────────────────────────────────────

class AcpTradeRequest(BaseModel):
    """Trade request sent through ACP."""

    coin: str
    side: Literal["long", "short"]
    size_usd: float
    leverage: int = 1
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rationale: str = ""
    idempotency_key: str = ""


class AcpTradeResponse(BaseModel):
    """Response from an ACP job."""

    success: bool
    job_id: str | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    failure_kind: Literal["retryable", "non_retryable"] | None = None
    #: Which execution backend filled the order (for venue TP/SL wallet selection).
    filled_via: Literal["senpi", "degen"] | None = None


class AcpCloseRequest(BaseModel):
    """Request to close a position through ACP."""

    coin: str
    rationale: str = ""
    idempotency_key: str = ""


# ── main ACP client ─────────────────────────────────────────────────────────

class DegenClawAcp:
    """Submits trade jobs through ACP v2 (Node buyer service) or legacy CLI.

    Modes:
      1. Dry-run — no SDK URL and no CLI
      2. SDK — ``ACP_SDK_URL`` or ``acp.sdk_url`` in config points at buyer HTTP
      3. Legacy CLI — ``npx tsx bin/acp.ts`` in ``ACP_CLI_DIR``
    """

    def __init__(self, cli_dir: str = "", config: dict[str, Any] | None = None, **_kwargs: Any):
        raw_dir = cli_dir or os.getenv(
            "ACP_CLI_DIR",
            "",
        )
        self._cli_dir = os.path.expanduser(raw_dir)
        self._config: dict[str, Any] = dict(config) if config else {}
        acp_cfg = self._config.get("acp") if isinstance(self._config.get("acp"), dict) else {}

        self._min_trade = float(
            acp_cfg.get("min_trade_size_usd", 10.0),
        )
        self._sdk_timeout = float(acp_cfg.get("http_timeout_seconds", 120.0))
        self._offering_name = (
            os.getenv("ACP_OFFERING_NAME", "").strip()
            or str(acp_cfg.get("offering_name") or "perp_trade")
        )
        self._sdk_url = (
            os.getenv("ACP_SDK_URL", "").strip().rstrip("/")
            or str(acp_cfg.get("sdk_url") or "").strip().rstrip("/")
        )

        self._pending_jobs: dict[str, AcpTradeRequest] = {}
        self._completed_jobs: dict[str, AcpTradeResponse] = {}
        self._provider_address = os.getenv("ACP_PROVIDER_WALLET", DEFAULT_PROVIDER_WALLET)

        acp_ts = os.path.join(self._cli_dir, "bin", "acp.ts")
        self._cli_available = os.path.isfile(acp_ts)

        if self._sdk_url:
            self._backend: Literal["sdk", "cli", "dry"] = "sdk"
            self._live = True
            logger.info(
                "ACP_ DegenClawAcp backend=sdk url=%s offering=%s",
                self._sdk_url,
                self._offering_name,
            )
        elif self._cli_available:
            self._backend = "cli"
            self._live = True
            logger.info("ACP_ DegenClawAcp backend=cli dir=%s", self._cli_dir)
        else:
            self._backend = "dry"
            self._live = False
            logger.warning(
                "ACP_ DegenClawAcp backend=dry (set ACP_SDK_URL or ACP_CLI_DIR + bin/acp.ts)",
            )

    # ── CLI execution (legacy) ───────────────────────────────────────────────

    def _run_cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run an ACP CLI command."""
        cmd = ["npx", "tsx", "bin/acp.ts", *args]
        logger.debug("ACP_ CLI: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            cwd=self._cli_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _headers_sdk(self) -> dict[str, str]:
        sec = os.getenv("ACP_BUYER_SECRET", "").strip()
        if not sec:
            return {}
        return {"X-Acp-Buyer-Token": sec}

    def _create_job_sdk(self, requirements: dict[str, Any]) -> AcpTradeResponse:
        """Create job via acp-node-v2 buyer HTTP service."""
        if not self._provider_address:
            return AcpTradeResponse(
                success=False,
                error="ACP provider wallet not configured",
            )
        url = f"{self._sdk_url}/v1/job"
        payload: dict[str, Any] = {
            "requirementData": requirements,
            "offeringName": self._offering_name,
            "providerAddress": self._provider_address,
        }
        try:
            with httpx.Client(timeout=self._sdk_timeout) as client:
                r = client.post(url, json=payload, headers=self._headers_sdk())
        except httpx.TimeoutException:
            return AcpTradeResponse(success=False, error="ACP SDK HTTP timeout")
        except httpx.RequestError as e:
            return AcpTradeResponse(success=False, error=f"ACP SDK HTTP error: {e}")

        try:
            data = r.json()
        except json.JSONDecodeError:
            return AcpTradeResponse(
                success=False,
                error=f"ACP SDK invalid JSON: {r.text[:500]}",
            )

        if r.status_code >= 400 or not data.get("ok"):
            err = data.get("error") or r.text or f"HTTP {r.status_code}"
            logger.error("ACP_ SDK create_job failed: %s", err)
            return AcpTradeResponse(success=False, error=str(err))

        job_id = data.get("jobId")
        if job_id is not None:
            job_id = str(job_id)
        logger.info("ACP_ SDK job created job_id=%s", job_id)
        return AcpTradeResponse(success=True, job_id=job_id)

    def _create_job_cli(
        self, offering: str, requirements: dict[str, Any],
    ) -> AcpTradeResponse:
        """Create an ACP job via legacy CLI and return the response."""
        if not self._provider_address:
            return AcpTradeResponse(
                success=False,
                error="ACP provider wallet not configured",
            )

        req_json = json.dumps(requirements, default=str)

        try:
            result = self._run_cli(
                "job", "create",
                self._provider_address,
                offering,
                "--requirements", req_json,
                "--isAutomated", "true",
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode != 0:
                error_msg = stderr or stdout or f"CLI exited with code {result.returncode}"
                logger.error("ACP_ CLI error: %s", error_msg)
                return AcpTradeResponse(success=False, error=error_msg)

            combined = (stdout + "\n" + stderr).strip()
            job_id = extract_acp_job_id(combined)

            logger.info("ACP_ CLI job created: %s | output: %s", job_id, stdout[:200])
            return AcpTradeResponse(success=True, job_id=job_id)

        except subprocess.TimeoutExpired:
            return AcpTradeResponse(success=False, error="ACP CLI timed out")
        except FileNotFoundError:
            self._live = False
            return AcpTradeResponse(success=False, error="npx not found — is Node.js installed?")
        except Exception as e:
            return AcpTradeResponse(success=False, error=str(e))

    # ── trade submission ─────────────────────────────────────────────────────

    def submit_trade(self, request: AcpTradeRequest) -> AcpTradeResponse:
        """Submit a trade request through ACP."""
        if request.size_usd < self._min_trade:
            return AcpTradeResponse(
                success=False,
                error=(
                    f"Size ${request.size_usd:.2f} below minimum "
                    f"${self._min_trade:.2f}"
                ),
            )

        if request.leverage <= 0:
            return AcpTradeResponse(
                success=False,
                error="Invalid leverage: must be >= 1",
            )

        # Degen Claw SKILL.md format: action, pair, side, size (string), leverage
        requirements: dict[str, Any] = {
            "action": "open",
            "pair": request.coin,
            "side": request.side,
            "size": str(int(request.size_usd)),  # Degen Claw expects integer string
        }
        if request.leverage > 1:
            requirements["leverage"] = request.leverage

        if self._backend == "sdk":
            response = self._create_job_sdk(requirements)
        elif self._backend == "cli":
            response = self._create_job_cli(self._offering_name, requirements)
        else:
            job_id = f"dry_{request.coin}_{request.side}_{int(datetime.now(timezone.utc).timestamp())}"
            logger.info(
                "ACP_ DRY-RUN trade: %s %s %s $%.2f | %s",
                request.side, request.coin, request.order_type,
                request.size_usd, request.rationale,
            )
            response = AcpTradeResponse(success=True, job_id=job_id)

        if response.success and response.job_id:
            self._pending_jobs[response.job_id] = request

        if response.success:
            response = response.model_copy(update={"filled_via": "degen"})
        return response

    def submit_close(self, request: AcpCloseRequest) -> AcpTradeResponse:
        """Submit a position close request via ACP."""
        if not request.coin:
            return AcpTradeResponse(success=False, error="No coin specified for close")

        requirements = {"action": "close", "pair": request.coin}

        if self._backend == "sdk":
            response = self._create_job_sdk(requirements)
        elif self._backend == "cli":
            response = self._create_job_cli(self._offering_name, requirements)
        else:
            job_id = f"dry_close_{request.coin}_{int(datetime.now(timezone.utc).timestamp())}"
            logger.info("ACP_ DRY-RUN close: %s | %s", request.coin, request.rationale)
            response = AcpTradeResponse(success=True, job_id=job_id)

        return response

    # ── job queries ──────────────────────────────────────────────────────────

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Check a specific job's status (CLI) or minimal SDK metadata."""
        if self._backend == "dry":
            return {"phase": "DRY_RUN", "job_id": job_id}
        if self._backend == "sdk":
            return {
                "backend": "sdk",
                "job_id": job_id,
                "note": "Inspect job on Virtuals / chain; buyer service streams SSE",
            }

        try:
            result = self._run_cli("job", "status", job_id)
            return {"raw_output": result.stdout.strip(), "job_id": job_id}
        except Exception as e:
            return {"error": str(e), "job_id": job_id}

    def get_active_jobs(self) -> list[dict]:
        """Query ACP for active jobs (legacy CLI only)."""
        if self._backend != "cli":
            return []
        try:
            result = self._run_cli("job", "active")
            return [{"raw_output": result.stdout.strip()}]
        except Exception as e:
            logger.error("ACP_ Failed to get active ACP jobs: %s", e)
            return []

    # ── state ────────────────────────────────────────────────────────────────

    def get_pending_jobs(self) -> dict[str, AcpTradeRequest]:
        return dict(self._pending_jobs)

    def get_completed_jobs(self) -> dict[str, AcpTradeResponse]:
        return dict(self._completed_jobs)

    def get_acp_state(self) -> dict[str, Any]:
        return {
            "mode": "live" if self._live else "dry_run",
            "backend": self._backend,
            "sdk_url": self._sdk_url if self._backend == "sdk" else None,
            "cli_dir": self._cli_dir if self._backend == "cli" else None,
            "pending_jobs": len(self._pending_jobs),
            "completed_jobs": len(self._completed_jobs),
        }

    def process_pending_callbacks(self) -> None:
        """No-op — CLI approach doesn't use callbacks."""
        pass

    def discover_provider(self) -> str:
        """Return the configured ACP provider wallet address."""
        return self._provider_address

    def mark_completed(self, job_id: str, response: AcpTradeResponse | None = None) -> None:
        removed = self._pending_jobs.pop(job_id, None)
        if removed and response:
            self._completed_jobs[job_id] = response

    @property
    def plugin(self) -> None:
        """No plugin in CLI mode."""
        return None

    @property
    def client(self) -> None:
        """No client in CLI mode."""
        return None

    @property
    def is_live(self) -> bool:
        return self._live
