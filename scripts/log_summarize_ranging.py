#!/usr/bin/env python3
"""
Summarize strict-vs-legacy ranging and structure-gate metrics from NXFH01/AceVault logs.

Example:
  python scripts/log_summarize_ranging.py --since 2026-04-21T00:00:00
      --until 2026-04-22T00:00:00 /var/log/nxfh01/prod.log
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TextIO


def _warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def _parse_iso_z(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_LINE_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_EMBED_TS_RE = re.compile(
    r"(?:^|\s)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)


def _line_timestamp(line: str) -> datetime | None:
    s = line.strip()
    m = _LINE_TS_RE.match(s)
    if not m:
        m = _EMBED_TS_RE.search(s[:240])
        if not m:
            return None
    raw = m.group(1).replace(" ", "T", 1)
    return _parse_iso_z(raw)


def _parse_cli_bounds(since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    lo = _parse_iso_z(since) if since else None
    hi = _parse_iso_z(until) if until else None
    if since and lo is None:
        _warn(f"Could not parse --since {since!r}; disabling time filter for since.")
        lo = None
    if until and hi is None:
        _warn(f"Could not parse --until {until!r}; disabling time filter for until.")
        hi = None
    return lo, hi


def _line_in_window(ts: datetime | None, lo: datetime | None, hi: datetime | None) -> bool:
    if lo is None and hi is None:
        return True
    if ts is None:
        return False
    if lo is not None and ts < lo:
        return False
    if hi is not None and ts > hi:
        return False
    return True


_KEY_START_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=")


def parse_kv_tail(line: str, marker: str) -> dict[str, str]:
    """Extract key=value pairs from the substring after *marker* (marker included in search)."""
    i = line.find(marker)
    if i < 0:
        return {}
    s = line[i + len(marker) :].strip()
    out: dict[str, str] = {}
    pos = 0
    n = len(s)
    while pos < n:
        m = _KEY_START_RE.match(s, pos)
        if not m:
            pos += 1
            continue
        key = m.group(1)
        val_start = m.end()
        nxt = re.search(r" (?=[a-zA-Z_][a-zA-Z0-9_]*=)", s[val_start:])
        if nxt:
            val = s[val_start : val_start + nxt.start()].strip()
            pos = val_start + nxt.start() + 1
        else:
            val = s[val_start:].strip()
            pos = n
        out[key] = val
    return out


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except ValueError:
        return default


def parse_strict_fail_reason_counts(raw: str) -> dict[str, int]:
    raw = raw.strip()
    if not raw or raw == "{}":
        return {}
    if raw.startswith("{") and raw.endswith("}"):
        try:
            ev = ast.literal_eval(raw)
        except (SyntaxError, ValueError, MemoryError):
            return {}
        if not isinstance(ev, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in ev.items():
            if isinstance(k, str):
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    pass
        return out
    # comma-separated reason:count
    out2: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip()
        try:
            out2[k] = int(v.strip())
        except ValueError:
            continue
    return out2


def _merge_reason_dicts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + v
    return out


def _reason_dict_delta(prev: dict[str, int], cur: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    keys = set(prev) | set(cur)
    for k in keys:
        d = cur.get(k, 0) - prev.get(k, 0)
        if d > 0:
            out[k] = d
    return out


@dataclass
class RegimeSnapshotEvent:
    ts: datetime | None
    order: int
    cycles_legacy_ranging_true: int | None = None
    cycles_strict_ranging_evaluated: int | None = None
    cycles_strict_ranging_passed: int | None = None
    cycles_legacy_true_strict_false: int | None = None
    strict_fail_reason_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Summary:
    regime_events: list[RegimeSnapshotEvent] = field(default_factory=list)
    skipped_unparseable_ts: int = 0
    # entry cycle (sums — per-line values)
    sum_candidates_seen: int = 0
    sum_blocked_structure: int = 0
    entry_lines_with_block: int = 0
    last_cumulative_structure_block: int | None = None
    shadow_count: int = 0
    shadow_coin_counter: Counter[str] = field(default_factory=Counter)


def _apply_regime_line(
    summ: Summary,
    kv: dict[str, str],
    ts: datetime | None,
    order: int,
) -> None:
    ev = RegimeSnapshotEvent(
        ts=ts,
        order=order,
        cycles_legacy_ranging_true=_parse_int(kv["cycles_legacy_ranging_true"])
        if "cycles_legacy_ranging_true" in kv
        else None,
        cycles_strict_ranging_evaluated=_parse_int(kv["cycles_strict_ranging_evaluated"])
        if "cycles_strict_ranging_evaluated" in kv
        else None,
        cycles_strict_ranging_passed=_parse_int(kv["cycles_strict_ranging_passed"])
        if "cycles_strict_ranging_passed" in kv
        else None,
        cycles_legacy_true_strict_false=_parse_int(kv["cycles_legacy_true_strict_false"])
        if "cycles_legacy_true_strict_false" in kv
        else None,
        strict_fail_reason_counts=parse_strict_fail_reason_counts(
            kv.get("strict_fail_reason_counts", "")
        ),
    )
    summ.regime_events.append(ev)


def _finalize_regime_metrics(summ: Summary) -> dict[str, Any]:
    evs = sorted(
        summ.regime_events,
        key=lambda e: (
            e.ts is None,
            e.ts or datetime.min.replace(tzinfo=timezone.utc),
            e.order,
        ),
    )
    if not evs:
        return {
            "total_cycles_legacy_ranging_true": 0,
            "total_cycles_strict_ranging_evaluated": 0,
            "total_cycles_strict_ranging_passed": 0,
            "total_cycles_legacy_true_strict_false": 0,
            "strict_fail_reason_delta": {},
        }

    def delta_series(attr: str) -> int:
        pairs: list[tuple[int, int]] = []
        for e in evs:
            v = getattr(e, attr, None)
            if v is not None:
                pairs.append((e.order, int(v)))
        if len(pairs) < 2:
            return 0
        pairs.sort(key=lambda t: t[0])
        nums = [p[1] for p in pairs]
        return max(0, nums[-1] - nums[0])

    out: dict[str, Any] = {
        "total_cycles_legacy_ranging_true": delta_series("cycles_legacy_ranging_true"),
        "total_cycles_strict_ranging_evaluated": delta_series("cycles_strict_ranging_evaluated"),
        "total_cycles_strict_ranging_passed": delta_series("cycles_strict_ranging_passed"),
        "total_cycles_legacy_true_strict_false": delta_series("cycles_legacy_true_strict_false"),
    }
    # fail reasons: sum of positive deltas between consecutive non-empty snapshots
    reason_delta: dict[str, int] = {}
    prev: dict[str, int] | None = None
    for e in evs:
        cur = e.strict_fail_reason_counts
        if not cur:
            continue
        if prev is None:
            prev = dict(cur)
            continue
        d = _reason_dict_delta(prev, cur)
        reason_delta = _merge_reason_dicts(reason_delta, d)
        prev = dict(cur)
    out["strict_fail_reason_delta"] = reason_delta
    return out


def process_file(
    path: str,
    lo: datetime | None,
    hi: datetime | None,
    summ: Summary,
    order_base: int,
) -> int:
    order = order_base
    try:
        f: TextIO
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        _warn(f"Could not open {path!r}: {e}")
        return order_base
    with f:
        for raw_line in f:
            line = raw_line.lstrip("\ufeff").rstrip("\n\r")
            ts = _line_timestamp(line)
            if lo is not None or hi is not None:
                if ts is None:
                    summ.skipped_unparseable_ts += 1
                    continue
                if not _line_in_window(ts, lo, hi):
                    continue

            if "REGIME_METRICS_SNAPSHOT" in line:
                try:
                    kv = parse_kv_tail(line, "REGIME_METRICS_SNAPSHOT")
                    _apply_regime_line(summ, kv, ts, order)
                    order += 1
                except Exception as e:
                    _warn(f"{path}: REGIME_METRICS_SNAPSHOT parse error: {e}")
                continue

            if "ACEVAULT_RANGING_ENTRY_CYCLE" in line:
                try:
                    kv = parse_kv_tail(line, "ACEVAULT_RANGING_ENTRY_CYCLE")
                    seen = _parse_int(kv.get("ranging_candidates_seen", "0"))
                    blocked = _parse_int(kv.get("blocked_by_ranging_structure_gate", "0"))
                    summ.sum_candidates_seen += seen
                    summ.sum_blocked_structure += blocked
                    if blocked > 0:
                        summ.entry_lines_with_block += 1
                    if "cycles_with_ranging_structure_block" in kv:
                        summ.last_cumulative_structure_block = _parse_int(
                            kv["cycles_with_ranging_structure_block"], 0
                        )
                except Exception as e:
                    _warn(f"{path}: ACEVAULT_RANGING_ENTRY_CYCLE parse error: {e}")
                continue

            if "ACEVAULT_RANGING_SHADOW_CYCLE" in line:
                try:
                    kv = parse_kv_tail(line, "ACEVAULT_RANGING_SHADOW_CYCLE")
                    summ.shadow_count += 1
                    coins_raw = kv.get("top_coins", "")
                    for c in coins_raw.split(","):
                        c = c.strip()
                        if c:
                            summ.shadow_coin_counter[c] += 1
                except Exception as e:
                    _warn(f"{path}: ACEVAULT_RANGING_SHADOW_CYCLE parse error: {e}")
                continue

    return order


def print_summary(summ: Summary, top_n: int) -> None:
    rm = _finalize_regime_metrics(summ)
    leg = int(rm["total_cycles_legacy_ranging_true"])
    mis = int(rm["total_cycles_legacy_true_strict_false"])
    mismatch_rate = 100.0 * mis / max(leg, 1)

    print("=== Regime mismatch ===")
    print(
        "  (Counters from REGIME_METRICS_SNAPSHOT are cumulative per process; "
        "reported totals are last-minus-first across snapshots in the filtered window.)",
    )
    print(f"  total_cycles_legacy_ranging_true: {leg}")
    print(f"  total_cycles_strict_ranging_evaluated: {rm['total_cycles_strict_ranging_evaluated']}")
    print(f"  total_cycles_strict_ranging_passed: {rm['total_cycles_strict_ranging_passed']}")
    print(f"  total_cycles_legacy_true_strict_false: {mis}")
    print(f"  mismatch_rate: {mis} / {max(leg, 1)} = {mismatch_rate:.2f}%")
    print()

    reasons: dict[str, int] = rm["strict_fail_reason_delta"]
    total_fails = sum(reasons.values())
    print("=== Strict fail reasons ===")
    print(f"  (delta across REGIME_METRICS_SNAPSHOT observations in window; total fail events: {total_fails})")
    for r, c in sorted(reasons.items(), key=lambda x: (-x[1], x[0])):
        pct = 100.0 * c / max(total_fails, 1)
        print(f"  {r}: {c} ({pct:.2f}% of fail events)")
    if not reasons:
        print("  (no strict_fail_reason_counts deltas in window)")
    print()

    seen = summ.sum_candidates_seen
    blk = summ.sum_blocked_structure
    bf = 100.0 * blk / max(seen, 1)
    print("=== Structure gate impact ===")
    print(f"  total_ranging_candidates_seen: {seen}")
    print(f"  total_ranging_candidates_blocked_by_structure: {blk}")
    print(f"  blocked_fraction: {blk} / {max(seen, 1)} = {bf:.2f}%")
    print(f"  entry_cycles_with_blocked>0: {summ.entry_lines_with_block}")
    if summ.last_cumulative_structure_block is not None:
        print(
            f"  last_seen_cycles_with_ranging_structure_block (cumulative): "
            f"{summ.last_cumulative_structure_block}"
        )
    print()

    print("=== Shadow cycles ===")
    print(f"  ACEVAULT_RANGING_SHADOW_CYCLE count: {summ.shadow_count}")
    print(f"  Top {top_n} coins by shadow-cycle appearances:")
    for coin, cnt in summ.shadow_coin_counter.most_common(top_n):
        pct = 100.0 * cnt / max(summ.shadow_count, 1)
        print(f"    {coin}: {cnt} ({pct:.2f}% of shadow cycles)")
    if summ.skipped_unparseable_ts:
        print()
        print(
            f"(Note: {summ.skipped_unparseable_ts} lines skipped: no parseable line timestamp "
            f"while --since/--until were set.)",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("logfiles", nargs="+", help="One or more log file paths")
    p.add_argument("--since", help="ISO datetime inclusive lower bound (UTC if naive)")
    p.add_argument("--until", help="ISO datetime inclusive upper bound (UTC if naive)")
    p.add_argument("--top-n", type=int, default=15, help="Top N shadow coins (default 15)")
    args = p.parse_args(argv)

    lo, hi = _parse_cli_bounds(args.since, args.until)
    if lo and hi and lo > hi:
        _warn("--since is after --until; swapping.")
        lo, hi = hi, lo

    summ = Summary()
    order = 0
    for path in args.logfiles:
        order = process_file(path, lo, hi, summ, order)

    print_summary(summ, max(1, args.top_n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
