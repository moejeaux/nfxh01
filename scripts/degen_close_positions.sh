#!/usr/bin/env bash
# Close Hyperliquid perps via Degen Claw / ACP — same contract as src/acp/degen_claw.py submit_close().
#
# Quick start (after env is set — see scripts/DEGEN_CLOSE_POSITIONS.md):
#   ./scripts/degen_close_positions.sh --dry-run BTC SOL
#   ./scripts/degen_close_positions.sh BTC SOL
#
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]]; then
  DRY_RUN=1
  shift
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 [--dry-run|-n] COIN [COIN...]" >&2
  echo "  Submits one ACP job per coin: requirementData = {\"action\":\"close\",\"pair\":\"COIN\"}" >&2
  echo "  Requires: ACP_CLI_DIR, ACP_PROVIDER_WALLET (see scripts/DEGEN_CLOSE_POSITIONS.md)" >&2
  exit 1
fi

if [[ -z "${ACP_CLI_DIR:-}" ]]; then
  echo "ERROR: ACP_CLI_DIR is not set (path to ACP checkout containing bin/acp.ts)." >&2
  exit 1
fi
if [[ -z "${ACP_PROVIDER_WALLET:-}" ]]; then
  echo "ERROR: ACP_PROVIDER_WALLET is not set." >&2
  exit 1
fi

OFFERING="${ACP_OFFERING_NAME:-perp_trade}"
ACP_TS="${ACP_CLI_DIR%/}/bin/acp.ts"
if [[ ! -f "$ACP_TS" ]]; then
  echo "ERROR: missing ${ACP_TS} — fix ACP_CLI_DIR." >&2
  exit 1
fi

failures=0
for coin in "$@"; do
  case "$coin" in
    *" "*|*"	"*)
      echo "ERROR: invalid coin token (whitespace): ${coin}" >&2
      failures=$((failures + 1))
      continue
      ;;
  esac
  # JSON for ACP --requirements (must match DegenClawAcp.submit_close)
  req=$(printf '{"action":"close","pair":"%s"}' "$coin")
  echo "RISK_MANUAL_DEGEN_CLOSE coin=${coin} offering=${OFFERING} dry_run=${DRY_RUN}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  would run: (cd \"${ACP_CLI_DIR}\" && npx tsx bin/acp.ts job create \"<PROVIDER>\" \"${OFFERING}\" --requirements '${req}' --isAutomated true)"
    continue
  fi
  set +e
  out=$(cd "$ACP_CLI_DIR" && npx tsx bin/acp.ts job create "$ACP_PROVIDER_WALLET" "$OFFERING" \
    --requirements "$req" --isAutomated true 2>&1)
  rc=$?
  set -e
  echo "$out"
  if [[ "$rc" -ne 0 ]]; then
    echo "RISK_MANUAL_DEGEN_CLOSE_FAILED coin=${coin} exit=${rc}" >&2
    failures=$((failures + 1))
  fi
done

if [[ "$failures" -gt 0 ]]; then
  exit 1
fi
