"""Postgres-backed config version registry with idempotent registration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from src.config_intelligence.diff import diff_versions
from src.config_intelligence.hashing import fingerprint_merged_config
from src.config_intelligence.holder import get_active_holder
from src.config_intelligence.normalize import canonicalize_for_hash

logger = logging.getLogger(__name__)


@dataclass
class RegisterResult:
    version_id: str | None
    is_new: bool
    events_emitted: int


def _ci_section(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("config_intelligence")
    return raw if isinstance(raw, dict) else {}


def _is_enabled(config: dict[str, Any]) -> bool:
    return bool(_ci_section(config).get("enabled", True))


def _environment(ci: dict[str, Any]) -> str:
    return (
        (os.getenv("NXFH01_ENV") or "").strip()
        or str(ci.get("environment") or "live").strip()
    )


def _venue(ci: dict[str, Any], merged: dict[str, Any]) -> str:
    v = str(ci.get("venue") or "").strip()
    if v:
        return v
    hl = merged.get("hyperliquid_api")
    if isinstance(hl, dict):
        url = str(hl.get("api_base_url") or "")
        if "hyperliquid" in url.lower():
            return "hyperliquid"
    return "default"


def _strategy_fingerprint_rows(
    canonical: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return (strategy_key, fingerprint_hex, pruned_config) tuples."""
    from src.config_intelligence.hashing import fingerprint_sha256_from_canonical

    rows: list[tuple[str, str, dict[str, Any]]] = []
    for key in (
        "acevault",
        "risk",
        "opportunity",
        "execution",
        "retro",
        "learning",
        "fathom",
        "orchestration",
        "universe",
    ):
        block = canonical.get(key)
        if isinstance(block, dict):
            cblock = canonicalize_for_hash(block)
            assert isinstance(cblock, dict)
            fh = fingerprint_sha256_from_canonical(cblock)
            rows.append((key, fh, cblock))
    strategies = canonical.get("strategies")
    if isinstance(strategies, dict):
        for sk, block in strategies.items():
            if isinstance(block, dict):
                cblock = canonicalize_for_hash(block)
                assert isinstance(cblock, dict)
                fh = fingerprint_sha256_from_canonical(cblock)
                rows.append((f"strategy:{sk}", fh, cblock))
    return rows


def _short_summary(canonical: dict[str, Any], h: str) -> str:
    keys = list(canonical.keys())[:12]
    return f"hash={h[:12]}… keys={','.join(keys)}"


async def register_active_version(
    pool: asyncpg.Pool | None,
    merged_config: dict[str, Any],
    *,
    created_by: str | None = None,
    learning_change_id: str | None = None,
) -> RegisterResult:
    """
    If ``pool`` is None or subsystem disabled, no-op (holder unchanged for pool=None).

    Uses advisory transaction lock + unique (environment, venue, config_hash).
    """
    if pool is None:
        logger.info("CONFIG_INTELLIGENCE_SKIP reason=no_db_pool")
        return RegisterResult(version_id=None, is_new=False, events_emitted=0)

    if not _is_enabled(merged_config):
        logger.info("CONFIG_INTELLIGENCE_SKIP reason=disabled_in_config")
        return RegisterResult(version_id=None, is_new=False, events_emitted=0)

    ci = _ci_section(merged_config)
    environment = _environment(ci)
    venue = _venue(ci, merged_config)
    canonical, h = fingerprint_merged_config(merged_config)
    git_sha = (os.getenv("GIT_COMMIT_SHA") or os.getenv("GITHUB_SHA") or "").strip() or None
    try:
        from src.nxfh01.runtime import VERSION as app_version  # type: ignore[attr-defined]
    except Exception:
        app_version = None

    source_paths: dict[str, Any] = {"primary": "config.yaml", "merged": True}

    category_rules = ci.get("category_prefixes")
    if not isinstance(category_rules, list):
        category_rules = []
    bundle_rules = ci.get("semantic_bundles")
    if not isinstance(bundle_rules, list):
        bundle_rules = []

    lock_key = f"config_version:{environment}:{venue}"

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
                lock_key,
            )
            row = await conn.fetchrow(
                """
                SELECT id::text FROM config_versions
                WHERE environment = $1 AND venue = $2 AND config_hash = $3
                """,
                environment,
                venue,
                h,
            )
            if row:
                vid = str(row["id"])
                holder = get_active_holder()
                holder.update(
                    version_id=vid,
                    config_hash=h,
                    environment=environment,
                    venue=venue,
                    applied_at=datetime.now(timezone.utc),
                )
                logger.info(
                    "CONFIG_INTELLIGENCE_VERSION_DEDUP version_id=%s hash=%s…",
                    vid,
                    h[:12],
                )
                return RegisterResult(version_id=vid, is_new=False, events_emitted=0)

            prev = await conn.fetchrow(
                """
                SELECT id::text, normalized_config
                FROM config_versions
                WHERE environment = $1 AND venue = $2
                ORDER BY applied_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                environment,
                venue,
            )

            summary = _short_summary(canonical, h)
            applied_at = datetime.now(timezone.utc)
            ins = await conn.fetchrow(
                """
                INSERT INTO config_versions (
                    applied_at, environment, venue, strategy_scope,
                    config_hash, git_commit_sha, app_version, source_paths,
                    normalized_config, summary, created_by
                ) VALUES ($1, $2, $3, NULL, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10)
                RETURNING id::text
                """,
                applied_at,
                environment,
                venue,
                h,
                git_sha,
                str(app_version) if app_version else None,
                json.dumps(source_paths),
                json.dumps(canonical, default=str),
                summary,
                created_by,
            )
            vid = str(ins["id"]) if ins else None
            if not vid:
                logger.error("CONFIG_INTELLIGENCE_INSERT_FAILED reason=no_returning_id")
                return RegisterResult(version_id=None, is_new=True, events_emitted=0)

            events_emitted = 0
            if prev and prev["normalized_config"] is not None:
                old_c = prev["normalized_config"]
                if isinstance(old_c, str):
                    old_c = json.loads(old_c)
                if not isinstance(old_c, dict):
                    old_c = {}
                drafts = diff_versions(
                    old_c,
                    canonical,
                    category_rules=category_rules,  # type: ignore[arg-type]
                    bundle_rules=bundle_rules,  # type: ignore[arg-type]
                )
                prev_id = str(prev["id"])
                for ev in drafts:
                    await conn.execute(
                        """
                        INSERT INTO config_change_events (
                            config_version_id, previous_config_version_id, detected_at,
                            path, old_value, new_value, value_type, change_category,
                            change_tags, change_kind, git_commit_sha, learning_change_id
                        ) VALUES (
                            $1::uuid, $2::uuid, NOW(), $3, $4::jsonb, $5::jsonb,
                            $6, $7, $8::text[], $9, $10, $11::uuid
                        )
                        """,
                        vid,
                        prev_id,
                        ev["path"],
                        json.dumps(ev.get("old_value"), default=str),
                        json.dumps(ev.get("new_value"), default=str),
                        ev.get("value_type"),
                        ev.get("change_category", "misc"),
                        ev.get("change_tags") or [],
                        ev.get("change_kind", "leaf"),
                        git_sha,
                        learning_change_id,
                    )
                    events_emitted += 1

            for sk, fh, pruned in _strategy_fingerprint_rows(canonical):
                await conn.execute(
                    """
                    INSERT INTO config_version_strategy_fingerprints (
                        version_id, strategy_key, fingerprint_hash, pruned_config
                    ) VALUES ($1::uuid, $2, $3, $4::jsonb)
                    """,
                    vid,
                    sk,
                    fh,
                    json.dumps(pruned, default=str),
                )

    holder = get_active_holder()
    holder.update(
        version_id=vid,
        config_hash=h,
        environment=environment,
        venue=venue,
        applied_at=applied_at,
    )
    logger.info(
        "CONFIG_INTELLIGENCE_VERSION_REGISTERED version_id=%s events=%d env=%s venue=%s",
        vid,
        events_emitted,
        environment,
        venue,
    )
    return RegisterResult(version_id=vid, is_new=True, events_emitted=events_emitted)


async def register_after_hot_reload(
    pool: asyncpg.Pool | None,
    merged_config: dict[str, Any],
    *,
    created_by: str | None = "hot_reload",
) -> RegisterResult:
    """Same as ``register_active_version`` — alias for clarity at merge call sites."""
    return await register_active_version(
        pool, merged_config, created_by=created_by, learning_change_id=None
    )
