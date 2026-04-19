# Close Hyperliquid perps via Degen Claw / ACP (Windows).
# Same contract as src/acp/degen_claw.py submit_close().
# Full instructions: scripts/DEGEN_CLOSE_POSITIONS.md
#
# Examples:
#   .\scripts\degen_close_positions.ps1 -DryRun BTC,SOL
#   .\scripts\degen_close_positions.ps1 BTC SOL

param(
    [switch]$DryRun,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Coins
)

$ErrorActionPreference = "Stop"

if (-not $Coins -or $Coins.Count -eq 0) {
    Write-Error "Usage: .\scripts\degen_close_positions.ps1 [-DryRun] COIN [COIN...]"
}

if (-not $env:ACP_CLI_DIR) { Write-Error "ACP_CLI_DIR is not set." }
if (-not $env:ACP_PROVIDER_WALLET) { Write-Error "ACP_PROVIDER_WALLET is not set." }

$offering = if ($env:ACP_OFFERING_NAME) { $env:ACP_OFFERING_NAME } else { "perp_trade" }
$acpTs = Join-Path $env:ACP_CLI_DIR "bin\acp.ts"
if (-not (Test-Path -LiteralPath $acpTs)) {
    Write-Error "Missing $acpTs — fix ACP_CLI_DIR."
}

$failures = 0
foreach ($coin in $Coins) {
    if ($coin -match '\s') {
        Write-Warning "RISK_MANUAL_DEGEN_CLOSE_FAILED invalid_coin=$coin"
        $failures++
        continue
    }
    $req = "{`"action`":`"close`",`"pair`":`"$coin`"}"
    Write-Host "RISK_MANUAL_DEGEN_CLOSE coin=$coin offering=$offering dry_run=$($DryRun.IsPresent)"
    if ($DryRun) {
        Write-Host "  would run: npx tsx bin/acp.ts job create ... --requirements $req"
        continue
    }
    Push-Location $env:ACP_CLI_DIR
    try {
        npx tsx bin/acp.ts job create $env:ACP_PROVIDER_WALLET $offering --requirements $req --isAutomated true
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "RISK_MANUAL_DEGEN_CLOSE_FAILED coin=$coin exit=$LASTEXITCODE"
            $failures++
        }
    }
    finally {
        Pop-Location
    }
}

if ($failures -gt 0) { exit 1 }
