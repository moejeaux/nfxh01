# Degen Claw: manual close (perp positions)

This runbook describes how to **close** Hyperliquid perpetual positions through **Degen Claw / ACP**, using the **same job contract** as the trading stack (`DegenClawAcp.submit_close` in [`src/acp/degen_claw.py`](../src/acp/degen_claw.py)).

Closing is expressed as an ACP job with:

```json
{"action": "close", "pair": "<COIN>"}
```

where `<COIN>` is the Hyperliquid perp symbol (e.g. `BTC`, `ETH`). This applies whether the position is long or short: the worker closes the pair.

These scripts **do not** query Hyperliquid for open positions. You choose symbols (from the HL UI, your logs, or another tool), then run one job per symbol.

---

## Prerequisites

1. **Node.js** and **npx** on the machine where you run the CLI (the ACP repo uses `npx tsx bin/acp.ts`).
2. **ACP checkout** (Virtuals / Degen Claw buyer CLI layout) with `bin/acp.ts` present.
3. **Environment variables** (same semantics as NXFH01 Python bootstrap):
   - **`ACP_CLI_DIR`** — absolute path to the ACP repository root (directory that contains `bin/acp.ts`).
   - **`ACP_PROVIDER_WALLET`** — provider wallet address passed to `job create` (must match what your buyer/offering expects).
   - **`ACP_OFFERING_NAME`** (optional) — ACP offering name; defaults to **`perp_trade`** if unset (matches typical `config.yaml` `acp.offering_name`).

Optional (only for SDK HTTP mode — see below):

- **`ACP_SDK_URL`** — not used by these shell scripts; they only implement the **CLI** path.
- **`ACP_BUYER_SECRET`** — header for buyer HTTP when using `curl` to `POST /v1/job`.

---

## Bash (Linux / macOS)

### 1. Export environment

```bash
export ACP_CLI_DIR="/path/to/your/acp-repo"
export ACP_PROVIDER_WALLET="0xYourProviderAddress"
# optional:
export ACP_OFFERING_NAME="perp_trade"
```

Load from `.env` if you prefer:

```bash
set -a && source /path/to/nxfh01/.env && set +a
```

Ensure `ACP_CLI_DIR` and `ACP_PROVIDER_WALLET` are set after sourcing.

### 2. Dry run (prints what would run; no jobs created)

```bash
chmod +x scripts/degen_close_positions.sh   # once
./scripts/degen_close_positions.sh --dry-run BTC SOL
```

### 3. Live close (creates one ACP job per coin)

```bash
./scripts/degen_close_positions.sh BTC SOL
```

Exit code is **non-zero** if any `job create` fails.

### 4. Log line convention

The script prints lines starting with **`RISK_MANUAL_DEGEN_CLOSE`** so they align with the project’s operator / risk logging style.

---

## PowerShell (Windows)

```powershell
$env:ACP_CLI_DIR = "C:\path\to\acp-repo"
$env:ACP_PROVIDER_WALLET = "0xYourProviderAddress"
$env:ACP_OFFERING_NAME = "perp_trade"   # optional

.\scripts\degen_close_positions.ps1 -DryRun BTC SOL
.\scripts\degen_close_positions.ps1 BTC SOL
```

---

## SDK / buyer HTTP (no local `bin/acp.ts`)

If NXFH01 is configured with **`ACP_SDK_URL`** (HTTP buyer sidecar) instead of the legacy CLI, Python uses `POST {ACP_SDK_URL}/v1/job` with a JSON body. Equivalent **`curl`** (replace placeholders):

```bash
export ACP_SDK_URL="https://your-buyer-host.example"
export ACP_PROVIDER_WALLET="0xYourProviderAddress"
export ACP_OFFERING_NAME="perp_trade"
COIN="BTC"

curl -sS -X POST "${ACP_SDK_URL%/}/v1/job" \
  -H "Content-Type: application/json" \
  ${ACP_BUYER_SECRET:+-H "X-Acp-Buyer-Token: ${ACP_BUYER_SECRET}"} \
  -d "$(jq -n \
    --arg offering "$ACP_OFFERING_NAME" \
    --arg provider "$ACP_PROVIDER_WALLET" \
    --arg coin "$COIN" \
    '{requirementData:{action:"close",pair:$coin},offeringName:$offering,providerAddress:$provider}')"
```

Expect a JSON response with **`ok": true`** and a **`jobId`** when successful (see `_create_job_sdk` in `degen_claw.py`). If `ok` is false, inspect the `error` field.

---

## How this relates to NXFH01

| Component | Role |
|-----------|------|
| [`DegenClawAcp.submit_close`](../src/acp/degen_claw.py) | Python builds the same `{"action":"close","pair": coin}` and calls CLI or SDK. |
| [`AceVaultEngine`](../src/engines/acevault/engine.py) | Calls `degen_executor.submit_close(...)` on exits. |
| These scripts | Operator-only manual path; **no** changes to `src/execution/` or `src/signal_ingress/`. |

If your runtime uses **SDK** mode (`ACP_SDK_URL` set, no `ACP_CLI_DIR`), use **`curl`** (or keep using the running NXFH01 process / future one-liner) rather than `degen_close_positions.sh`, unless you also have a valid CLI checkout.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| `missing .../bin/acp.ts` | `ACP_CLI_DIR` must point at the repo root that contains `bin/acp.ts`. |
| `ACP_PROVIDER_WALLET is not set` | Export the same provider address you use for production ACP jobs. |
| `npx: command not found` / `tsx` errors | Install Node.js; in the ACP repo, run `npm install` (or equivalent) per that project’s docs. |
| CLI exit non-zero | Read stderr/stdout from `job create`; verify offering name matches your Virtuals offering (`ACP_OFFERING_NAME`). |
| Wrong coin | Symbol must match Hyperliquid’s perp name in ACP/Degen Claw (case is passed through as you type). |
| Position did not close | Job creation ≠ fill: check ACP / buyer job status on chain or your buyer UI. |

---

## Safety checklist

1. Run **`--dry-run`** first and confirm the printed `pair` values.
2. Confirm **`ACP_PROVIDER_WALLET`** is the intended production provider.
3. Close **one** coin first in production if you are validating a new environment.
4. This does **not** cancel open limit orders or handle spot; only the **close** intent for the given perp `pair`.
