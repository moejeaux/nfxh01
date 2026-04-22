from __future__ import annotations

import pytest

from src.verification.shadow_report import ShadowReport


def _make_record(
    coin: str = "DOGE",
    regime: str = "trending_down",
    approved: bool = True,
    estimated_cost_bps: float = 5.0,
    reject_reason: str = "",
) -> dict:
    return {
        "coin": coin,
        "regime": regime,
        "expected_entry_price": 0.15,
        "estimated_cost_bps": estimated_cost_bps,
        "approved": approved,
        "reject_reason": reject_reason,
    }


class TestShadowReportRecordsSignals:
    def test_shadow_report_records_signals(self):
        report = ShadowReport()
        report.record(_make_record(coin="DOGE"))
        report.record(_make_record(coin="PEPE"))
        summary = report.summarize()
        assert summary["total_signals"] == 2


class TestShadowReportSummaryCountsApprovedAndRejected:
    def test_shadow_report_summary_counts_approved_and_rejected(self):
        report = ShadowReport()
        report.record(_make_record(approved=True))
        report.record(_make_record(approved=True))
        report.record(_make_record(approved=False, reject_reason="gross_exposure_limit"))
        summary = report.summarize()
        assert summary["approved_signals"] == 2
        assert summary["rejected_signals"] == 1


class TestShadowReportSummaryIncludesAvgEstimatedCost:
    def test_shadow_report_summary_includes_avg_estimated_cost(self):
        report = ShadowReport()
        report.record(_make_record(estimated_cost_bps=10.0))
        report.record(_make_record(estimated_cost_bps=20.0))
        summary = report.summarize()
        assert summary["avg_estimated_cost_bps"] == pytest.approx(15.0)


class TestScriptRespectsShadowModeFlag:
    def test_script_respects_shadow_mode_flag(self, tmp_path, monkeypatch):
        import asyncio
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "verification:\n  shadow_mode_enabled: false\nhyperliquid_api:\n  api_base_url: x\n"
        )
        monkeypatch.chdir(tmp_path)

        from scripts.run_shadow_mode import main

        result = asyncio.run(main())
        assert result == 1


class TestScriptDoesNotSubmitRealTrades:
    def test_script_does_not_submit_real_trades(self):
        import ast
        from pathlib import Path

        script_path = Path(__file__).resolve().parent.parent / "scripts" / "run_shadow_mode.py"
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        forbidden = {"place_order", "market_open", "submit_order", "create_order"}
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                if name and name in forbidden:
                    found.append(name)

        assert found == [], f"Forbidden trade-submission calls found: {found}"
