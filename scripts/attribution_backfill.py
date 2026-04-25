#!/usr/bin/env python3
"""
Mark legacy trades with attribution_tier=inferred|unknown (operator workflow).

Requires DATABASE_URL and applied migration 008. Does not fabricate config_version FKs
without operator-supplied mapping (tier=unknown by default).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg


async def _run(args: argparse.Namespace) -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL required", file=sys.stderr)
        return 1
    conn = await asyncpg.connect(url)
    try:
        tier = args.tier
        actor = args.actor or "attribution_backfill_script"
        details = {"note": args.note or "", "batch": args.batch_label}
        if args.trade_table == "acevault":
            q = """
            SELECT id FROM acevault_decisions
            WHERE decision_type = 'entry'
              AND outcome_recorded_at IS NOT NULL
              AND entry_config_version_id IS NULL
            LIMIT $1
            """
            rows = await conn.fetch(q, args.limit)
            for r in rows:
                tid = r["id"]
                await conn.execute(
                    """
                    INSERT INTO trade_attribution (
                        trade_table, trade_id, attribution_tier, cohorts,
                        inference_notes, created_at, updated_at
                    ) VALUES (
                        'acevault', $1::uuid, $2, $3::jsonb, $4, NOW(), NOW()
                    )
                    ON CONFLICT (trade_table, trade_id) DO UPDATE SET
                        attribution_tier = EXCLUDED.attribution_tier,
                        cohorts = EXCLUDED.cohorts,
                        inference_notes = EXCLUDED.inference_notes,
                        updated_at = NOW()
                    """,
                    tid,
                    tier,
                    json.dumps({"backfill": True}, default=str),
                    args.note,
                )
                await conn.execute(
                    """
                    INSERT INTO attribution_backfill_audit (
                        trade_table, trade_id, tier, actor, details
                    ) VALUES ($1, $2::uuid, $3, $4, $5::jsonb)
                    """,
                    "acevault",
                    tid,
                    tier,
                    actor,
                    json.dumps(details, default=str),
                )
        else:
            print("Only acevault backfill supported in this version", file=sys.stderr)
            return 2
        print("processed", len(rows), "rows tier=", tier)
        return 0
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tier", choices=("unknown", "inferred"), default="unknown")
    p.add_argument("--trade-table", default="acevault")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--actor", default="")
    p.add_argument("--note", default="")
    p.add_argument("--batch-label", default="")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
