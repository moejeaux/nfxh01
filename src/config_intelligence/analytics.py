"""Read-side helpers for config intelligence (SQL via asyncpg)."""

from __future__ import annotations

from typing import Any

import asyncpg


async def diff_config_versions(
    pool: asyncpg.Pool,
    version_a: str,
    version_b: str,
) -> list[dict[str, Any]]:
    """Return change events recorded for ``version_b`` (``version_a`` reserved for filters)."""
    _ = version_a
    q = """
    SELECT path, old_value, new_value, change_category, change_kind, detected_at
    FROM config_change_events
    WHERE config_version_id = $1::uuid
    ORDER BY path
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(q, version_b)
    return [dict(r) for r in rows]


async def list_change_events(
    pool: asyncpg.Pool,
    *,
    config_version_id: str | None = None,
    change_category: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    i = 1
    if config_version_id:
        clauses.append(f"config_version_id = ${i}::uuid")
        args.append(config_version_id)
        i += 1
    if change_category:
        clauses.append(f"change_category = ${i}")
        args.append(change_category)
        i += 1
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    q = f"""
    SELECT id, config_version_id, previous_config_version_id, path, change_category,
           change_kind, detected_at, value_type
    FROM config_change_events
    {where}
    ORDER BY detected_at DESC
    LIMIT {int(limit)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(q, *args)
    return [dict(r) for r in rows]


async def get_trade_attribution(
    pool: asyncpg.Pool,
    *,
    trade_table: str,
    trade_id: str,
) -> dict[str, Any] | None:
    q = """
    SELECT ta.*, cv.config_hash AS entry_version_config_hash, cv.summary AS entry_version_summary
    FROM trade_attribution ta
    LEFT JOIN config_versions cv ON cv.id = ta.entry_config_version_id
    WHERE ta.trade_table = $1 AND ta.trade_id = $2::uuid
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(q, trade_table, trade_id)
    return dict(row) if row else None


async def get_pre_post_analysis(
    pool: asyncpg.Pool,
    *,
    config_version_pre: str,
    config_version_post: str,
    min_trades: int = 20,
) -> dict[str, Any]:
    """Compare closed AceVault trades by entry_config_version_id (two cohorts)."""
    metric_sql = """
    SELECT
      entry_config_version_id::text AS vid,
      COUNT(*)::bigint AS n,
      AVG(COALESCE(net_pnl_usd, pnl_usd - COALESCE(fee_paid_usd, 0))) AS avg_net,
      SUM(COALESCE(net_pnl_usd, pnl_usd - COALESCE(fee_paid_usd, 0))) AS sum_net
    FROM acevault_decisions
    WHERE decision_type = 'entry'
      AND outcome_recorded_at IS NOT NULL
      AND entry_config_version_id = $1::uuid
    GROUP BY entry_config_version_id
    """
    async with pool.acquire() as conn:
        pre = await conn.fetchrow(metric_sql, config_version_pre)
        post = await conn.fetchrow(metric_sql, config_version_post)
    pre_d = dict(pre) if pre else {}
    post_d = dict(post) if post else {}
    n_pre = int(pre_d.get("n") or 0)
    n_post = int(post_d.get("n") or 0)
    return {
        "pre": pre_d,
        "post": post_d,
        "sufficiency_pre": "insufficient" if n_pre < min_trades else "ok",
        "sufficiency_post": "insufficient" if n_post < min_trades else "ok",
        "min_trades": min_trades,
    }


def get_change_impact_sql_template() -> str:
    """Reference for operators: primary slice view name."""
    return "v_profitability_by_config_version"
