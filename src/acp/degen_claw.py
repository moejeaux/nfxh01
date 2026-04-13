"""ACP wrapper for trade submission via the ACP CLI.

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

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_WALLET = ""
MIN_TRADE_SIZE_USD = 10.0


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
    """Submits trade jobs through the ACP CLI.

    Two modes:
      1. Dry-run (no ACP_CLI_DIR or CLI not found) — logs trades locally
      2. Live (CLI available) — shells out to `npx tsx bin/acp.ts`
    """

    def __init__(self, cli_dir: str = "", **_kwargs: Any):
        raw_dir = cli_dir or os.getenv(
            "ACP_CLI_DIR",
            "",
        )
        self._cli_dir = os.path.expanduser(raw_dir)
        self._pending_jobs: dict[str, AcpTradeRequest] = {}
        self._completed_jobs: dict[str, AcpTradeResponse] = {}
        self._provider_address = os.getenv("ACP_PROVIDER_WALLET", DEFAULT_PROVIDER_WALLET)

        # Check if CLI exists
        acp_ts = os.path.join(self._cli_dir, "bin", "acp.ts")
        self._live = os.path.isfile(acp_ts)

        if self._live:
            logger.info("ACP CLI found at %s", self._cli_dir)
        else:
            logger.warning(
                "ACP CLI not found at %s — running in DRY-RUN mode. "
                "Set ACP_CLI_DIR or install openclaw-acp.",
                acp_ts,
            )

    # ── CLI execution ────────────────────────────────────────────────────────

    def _run_cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run an ACP CLI command."""
        cmd = ["npx", "tsx", "bin/acp.ts", *args]
        logger.debug("ACP CLI: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            cwd=self._cli_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _create_job(
        self, offering: str, requirements: dict[str, Any],
    ) -> AcpTradeResponse:
        """Create an ACP job via CLI and return the response."""
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
                logger.error("ACP CLI error: %s", error_msg)
                return AcpTradeResponse(success=False, error=error_msg)

            # Extract job ID from output (e.g., "Job 1003224169 created")
            job_id = None
            match = re.search(r"Job\s+(\d+)", stdout)
            if match:
                job_id = match.group(1)

            logger.info("ACP job created: %s | output: %s", job_id, stdout[:200])
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
        if request.size_usd < MIN_TRADE_SIZE_USD:
            return AcpTradeResponse(
                success=False,
                error=f"Size ${request.size_usd:.2f} below minimum ${MIN_TRADE_SIZE_USD}",
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

        if self._live:
            response = self._create_job("perp_trade", requirements)
        else:
            job_id = f"dry_{request.coin}_{request.side}_{int(datetime.now(timezone.utc).timestamp())}"
            logger.info(
                "ACP DRY-RUN trade: %s %s %s $%.2f | %s",
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

        if self._live:
            response = self._create_job("perp_trade", requirements)
        else:
            job_id = f"dry_close_{request.coin}_{int(datetime.now(timezone.utc).timestamp())}"
            logger.info("ACP DRY-RUN close: %s | %s", request.coin, request.rationale)
            response = AcpTradeResponse(success=True, job_id=job_id)

        return response

    # ── job queries ──────────────────────────────────────────────────────────

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Check a specific job's status via CLI."""
        if not self._live:
            return {"phase": "DRY_RUN", "job_id": job_id}

        try:
            result = self._run_cli("job", "status", job_id)
            return {"raw_output": result.stdout.strip(), "job_id": job_id}
        except Exception as e:
            return {"error": str(e), "job_id": job_id}

    def get_active_jobs(self) -> list[dict]:
        """Query ACP for active jobs."""
        if not self._live:
            return []
        try:
            result = self._run_cli("job", "active")
            return [{"raw_output": result.stdout.strip()}]
        except Exception as e:
            logger.error("Failed to get active ACP jobs: %s", e)
            return []

    # ── state ────────────────────────────────────────────────────────────────

    def get_pending_jobs(self) -> dict[str, AcpTradeRequest]:
        return dict(self._pending_jobs)

    def get_completed_jobs(self) -> dict[str, AcpTradeResponse]:
        return dict(self._completed_jobs)

    def get_acp_state(self) -> dict[str, Any]:
        return {
            "mode": "live" if self._live else "dry_run",
            "cli_dir": self._cli_dir,
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
