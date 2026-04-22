# NXFH01 — Agent instructions

Repo: Hyperliquid perps trading system (AceVault + future engines). Treat live paths as high-risk; default to observability-first.

## Trading Lifecycle Placement

- **Regime detector**  
  - Produces market-state classification and snapshot metadata.  
  - May set flags used by downstream gates/policies.  
  - Must not directly submit, cancel, or close orders.

- **Scanner / ranker**  
  - Produces and scores candidate opportunities.  
  - May influence which symbols are considered.  
  - Must not directly execute trades.

- **Entry manager**  
  - Primary place for **pre-entry activation** logic.  
  - New gates that decide whether a position may be opened belong here.  
  - Examples: `ranging_structure_gate`, midpoint no-trade zone, edge-distance gate, cooldown after loss, expected-move-to-cost filter.

- **Exit policy / exit manager**  
  - Primary place for **in-trade exit** logic.  
  - New logic that changes how open positions are managed or closed belongs here.  
  - Examples: breakeven activation, ATR trailing, hard R cap, range-target exit, stale invalidation.

- **Metrics / observability**  
  - Logging, counters, snapshots, and shadow evaluation belong here.  
  - Observability code must not change live trading behavior unless the task explicitly says so.

Sacred paths (do not modify unless the user explicitly instructs): `src/execution/`, `src/signal_ingress/`.

## Activation Scope Rules

- Every implementation prompt should explicitly label each requested change as one of: **Observability only** | **Entry path activation** | **Exit path activation** | **Post-trade analytics**.
- Do not place trade-affecting logic in metrics modules.
- Do not place execution logic in detector or scanner modules.
- Do not place entry gating logic inside exit policy modules.
- Any live-behavior change must state whether it affects: **opening positions**, **managing open positions**, or **reporting only**.

Project rules still apply: thresholds belong in `config.yaml` (no silent hardcoding); log prefixes `ACEVAULT_`, `REGIME_`, `RISK_` where applicable; `UnifiedRiskLayer.validate()` gates orders — no bypass.

## Trading Safety Rails

- No partial closes unless explicitly requested.
- No hidden behavior changes inside logging/metrics refactors.
- No threshold changes unless the task explicitly asks for them.
- Prefer observability-first before relaxing or tightening live trading gates.
- Keep new thresholds/configs centralized and named clearly.
- Keep changes modular and backtestable.

## Prompting Pattern for This Repo

Reuse this skeleton when requesting trading work:

```text
Goal: <one sentence>

Scope: Observability only | Entry path activation | Exit path activation | Post-trade analytics

Activation placement: <regime / scanner / entry manager / exit policy / metrics — pick the primary layer>

Constraints: <e.g. no execution/ingress edits, no threshold edits, no partial closes>

Files/modules to modify: <paths>

Tests/verification: <pytest targets or manual checks>

Metrics to inspect after deployment: <logs, counters, reports>
```

## Ranging Strategy Notes

- Strict vs legacy ranging observability must stay separate from live entry behavior unless explicitly promoted.
- Ranging structure checks are **pre-entry gates** (entry manager / entry path), not regime-only side effects that execute or size trades.
- Realized-vs-peak R capture improvements belong in **exit** logic, not detector code.
- Shadow evaluation must be **logging-only** and must never submit orders.

## Related repo docs

- Rollout phases and what affects live orders: `docs/nxfh01-production-rollout.md`
- Pre-enable checks: `docs/nxfh01-preflight-checklist.md`
- Editor-wide constraints: `.cursor/rules/NXFH01-Project-Rules.mdc` (references this file for lifecycle placement)
