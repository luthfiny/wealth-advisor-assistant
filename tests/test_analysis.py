import unittest
from decimal import Decimal

from tests.helpers import client_raw, profile_raw
from wealth_advisor.analysis import AnalysisPolicy, analyze
from wealth_advisor.domain import ClientData, CRMProfile


def run_analysis(raw=None, profile=None, history=None):
    data = ClientData.parse(raw if raw is not None else client_raw())
    crm = CRMProfile.parse(
        profile if profile is not None else profile_raw(), data.client_id, data.as_of
    )
    return analyze(data, crm, history or [])


class AnalysisTests(unittest.TestCase):
    def test_policy_overrides_control_decisions_and_are_exported(self):
        data = ClientData.parse(client_raw())
        profile = CRMProfile.parse(profile_raw(), data.client_id, data.as_of)
        policy = AnalysisPolicy(
            version="custom-test-policy",
            moderate_equity_limit=Decimal("0.95"),
            single_asset_limit=Decimal("0.90"),
            emergency_months=Decimal("1"),
            absolute_outflow=Decimal("20000"),
        )
        metrics, findings = analyze(data, profile, [], policy)
        self.assertTrue(
            {
                "RISK_PROFILE_MISMATCH",
                "SINGLE_ASSET_CONCENTRATION",
                "LOW_LIQUIDITY",
                "LARGE_OUTFLOW",
            }.isdisjoint(f.code for f in findings)
        )
        self.assertEqual(metrics["policy"]["version"], "custom-test-policy")
        self.assertEqual(metrics["policy"]["absolute_outflow"], Decimal("20000"))

    def test_sample_balance_sheet_and_cash_coverage(self):
        metrics, _ = run_analysis()
        self.assertEqual(metrics["total_assets"], Decimal("212000.00"))
        self.assertEqual(metrics["net_worth"], Decimal("182000.00"))
        self.assertEqual(metrics["cash_runway_months"], Decimal("2.00"))
        self.assertEqual(metrics["declared_monthly_surplus"], Decimal("3000.00"))
        self.assertEqual(metrics["equity_share_of_investments"], Decimal("0.90"))

    def test_sample_expected_flags_and_evidence(self):
        _, findings = run_analysis()
        self.assertEqual(
            {f.code for f in findings},
            {
                "LOW_LIQUIDITY",
                "SINGLE_ASSET_CONCENTRATION",
                "RISK_PROFILE_MISMATCH",
                "LARGE_OUTFLOW",
                "UNUSUAL_TRANSACTION",
                "SPENDING_GROWTH",
                "LIMITED_ANOMALY_BASELINE",
            },
        )
        self.assertEqual(len(findings), 8)  # Two separate spending categories.
        spike = next(f for f in findings if f.code == "UNUSUAL_TRANSACTION")
        self.assertEqual(spike.evidence["transaction_id"], "sep_spike")
        self.assertEqual(spike.evidence["median"], Decimal("100"))
        self.assertEqual(spike.evidence["threshold"], Decimal("500"))
        self.assertEqual(spike.evidence["baseline_count"], 16)

    def test_income_internal_transfers_and_refunds_are_not_outflows(self):
        raw = client_raw()
        for tx in raw["transactions"]:
            if tx["kind"] in {"income", "internal_transfer", "refund"}:
                tx["amount"] = "999999.00"
        _, findings = run_analysis(raw)
        flagged = {f.evidence.get("transaction_id") for f in findings}
        self.assertNotIn("sep_internal", flagged)
        self.assertNotIn("sep_salary", flagged)
        self.assertNotIn("sep_refund", flagged)

    def test_zero_mad_still_detects_spike(self):
        raw = client_raw()
        for tx in raw["transactions"]:
            if tx["category"] == "groceries" and tx["date"] < "2026-09-01":
                tx["amount"] = "100.00"
        _, findings = run_analysis(raw)
        spike = next(f for f in findings if f.code == "UNUSUAL_TRANSACTION")
        self.assertEqual(spike.evidence["median_absolute_deviation"], Decimal("0"))

    def test_spike_threshold_boundary_is_strict(self):
        raw = client_raw()
        next(t for t in raw["transactions"] if t["id"] == "sep_spike")["amount"] = "500.00"
        _, findings = run_analysis(raw)
        self.assertNotIn("UNUSUAL_TRANSACTION", {f.code for f in findings})

    def test_absolute_threshold_is_inclusive(self):
        raw = client_raw()
        next(t for t in raw["transactions"] if t["id"] == "sep_wire")["amount"] = "10000.00"
        _, findings = run_analysis(raw)
        self.assertIn("LARGE_OUTFLOW", {f.code for f in findings})

    def test_small_baseline_suppresses_statistical_claims(self):
        raw = client_raw()
        raw["transactions"] = [t for t in raw["transactions"] if t["date"] >= "2026-09-01"]
        _, findings = run_analysis(raw)
        codes = {f.code for f in findings}
        self.assertNotIn("UNUSUAL_TRANSACTION", codes)
        self.assertIn("LIMITED_ANOMALY_BASELINE", codes)
        self.assertIn("LARGE_OUTFLOW", codes)

    def test_partial_history_suppresses_monthly_growth(self):
        raw = client_raw()
        raw["history_start"] = "2026-08-02"
        raw["transactions"] = [t for t in raw["transactions"] if t["date"] >= "2026-08-02"]
        metrics, findings = run_analysis(raw)
        self.assertFalse(metrics["spending_comparison"]["available"])
        self.assertNotIn("SPENDING_GROWTH", {f.code for f in findings})

    def test_monthly_growth_uses_complete_months(self):
        raw = client_raw()
        raw["as_of"] = "2026-10-15"
        raw["transactions"].append(
            {
                "id": "oct_dining",
                "date": "2026-10-03",
                "amount": "50000",
                "kind": "expense",
                "category": "dining",
                "counterparty": "Demo",
            }
        )
        _, findings = run_analysis(raw)
        growth = next(
            f
            for f in findings
            if f.code == "SPENDING_GROWTH" and f.evidence["category"] == "dining"
        )
        self.assertEqual(growth.evidence["latest_amount"], Decimal("1200"))
        self.assertEqual(growth.evidence["previous_amount"], Decimal("400"))

    def test_empty_portfolio_zero_expenses_and_zero_income_are_safe(self):
        raw = client_raw()
        raw.update(holdings=[], transactions=[], monthly_expenses="0", monthly_income="0")
        metrics, findings = run_analysis(raw)
        self.assertIsNone(metrics["cash_runway_months"])
        self.assertIsNone(metrics["equity_share_of_investments"])
        self.assertIn("EXPENSE_BASELINE_MISSING", {f.code for f in findings})

    def test_negative_budget_and_net_worth(self):
        raw = client_raw()
        raw.update(monthly_income="1000", liabilities="999999")
        _, findings = run_analysis(raw)
        self.assertTrue({"NEGATIVE_CASH_FLOW", "NEGATIVE_NET_WORTH"} <= {f.code for f in findings})

    def test_stale_unverified_profile_and_short_horizon(self):
        profile = profile_raw()
        profile.update(
            updated_at="2025-01-01",
            kyc_status="expired",
            risk_tolerance="aggressive",
            investment_horizon_years=1,
        )
        _, findings = run_analysis(profile=profile)
        self.assertTrue(
            {"STALE_PROFILE", "KYC_REVIEW", "RISK_PROFILE_MISMATCH"} <= {f.code for f in findings}
        )
        mismatch = next(f for f in findings if f.code == "RISK_PROFILE_MISMATCH")
        self.assertEqual(mismatch.evidence["limit"], Decimal("0.40"))

    def test_missing_crm_is_explicit_and_does_not_invent_profile(self):
        _, findings = analyze(ClientData.parse(client_raw()), None, [])
        self.assertIn("CRM_UNAVAILABLE", {f.code for f in findings})
        self.assertNotIn("RISK_PROFILE_MISMATCH", {f.code for f in findings})

    def test_history_comparison_flags_decline_without_claiming_cause(self):
        prior = {
            "run_id": "old_run",
            "as_of": "2026-09-01",
            "net_worth": "240000",
            "cash_runway_months": "6.00",
        }
        metrics, findings = run_analysis(history=[prior])
        self.assertEqual(metrics["historical_comparison"]["net_worth_change"], Decimal("-58000"))
        self.assertEqual(metrics["historical_comparison"]["cash_runway_change"], Decimal("-4.00"))
        self.assertIn("NET_WORTH_DROP", {f.code for f in findings})

    def test_finding_ids_and_order_are_stable(self):
        _, first = run_analysis()
        raw = client_raw()
        raw["transactions"].reverse()
        _, second = run_analysis(raw)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
