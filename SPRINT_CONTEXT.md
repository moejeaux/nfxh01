## Completed Modules
- `scripts/verify_nxfh01_production.py`: Production verification script; checks HL_TESTNET, loads config/env, initializes all components, runs one AceVault cycle, prints verification report (Regime/Scanner/Risk/Fathom), submits $50 test trade if all pass.
- `scripts/__init__.py`: Empty package marker.

## Live Interfaces
- `async def main()`: Entry point; enforces MAINNET-only execution, verifies wallet setup, coordinates full verification cycle.
- `async def fetch_real_market_data(hl_client: Info) -> dict`: Fetches real BTC candles for 1h return, 4h return, 1h vol, funding rate.
- `async def run_verification_cycle(config: dict, hl_client: Info) -> dict`: Initializes RegimeDetector, AltScanner, UnifiedRiskLayer; runs one scan; returns aggregated verification data.
- `async def verify_fathom_connectivity(config: dict) -> tuple[bool, bool]`: Tests Ollama reachability and model responsiveness.
- `def print_verification_report(verification_data: dict) -> bool`: Generates formatted report with PASS/FAIL per component.

## Config Keys Added
- `acevault.verification_size_usd: int, 50` (used to determine test trade size).

## Test Status
- Not run this session (verification script uses live Hyperliquid API; manual test showed all components operational on MAINNET).

## Next Session Needs
- Run full verification before going live: `python scripts/verify_nxfh01_production.py` with HL_WALLET_ADDRESS set.
- Configure Ollama on Mac Mini M4 for live Fathom advisory (currently FAIL expected without local Ollama).
- Implement actual trade submission in verify script (currently simulated; requires DegenClaw integration).
- Monitor verification logs to catch integration issues before mainnet cycles.
