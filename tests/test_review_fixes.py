"""Regressions for independent checks, canonical guards and replaceable tool ports."""

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.helpers import CRM, SAMPLE, client_raw, profile_raw
from wealth_advisor.agents import AnalyzerAgent
from wealth_advisor.analysis import Detector, DetectorRegistry, analyze_detailed
from wealth_advisor.config import Settings
from wealth_advisor.domain import ClientData, CRMProfile, Finding, PortfolioGuard, dumps
from wealth_advisor.orchestrator import OrchestratorAgent
from wealth_advisor.runtime import build_system
from wealth_advisor.tool_runtime import ToolClient, ToolMetadata, ToolObserver, ToolProvider


class CallOnlyClient:
    """Agents can use this without depending on registry or provider methods."""

    def __init__(self, delegate: ToolClient):
        self.delegate = delegate

    def call(self, name: str, arguments: dict, *, run_id: str):
        return self.delegate.call(name, arguments, run_id=run_id)


class ScopedProvider:
    """The orchestrator needs only scoped clients, not the concrete registry API."""

    def __init__(self, delegate: ToolProvider):
        self.delegate = delegate
        self.observed: list[ToolMetadata] = []

    def scoped(self, allowed, observer: ToolObserver | None = None) -> ToolClient:
        def observe(metadata: ToolMetadata) -> None:
            self.observed.append(metadata)
            if observer:
                observer(metadata)

        return CallOnlyClient(self.delegate.scoped(allowed, observe))


class AnalysisBoundaryTests(unittest.TestCase):
    def spike_data(self, amount):
        raw = client_raw()
        next(t for t in raw["transactions"] if t["id"] == "sep_spike")["amount"] = amount
        data = ClientData.parse(raw)
        return data, CRMProfile.parse(profile_raw(), data.client_id, data.as_of)

    def test_statistical_detector_retains_flags_above_absolute_threshold(self):
        for amount in ("9000.00", "20000.00"):
            with self.subTest(amount=amount):
                data, profile = self.spike_data(amount)
                result = analyze_detailed(data, profile, [], selected=["statistical_outflows"])
                self.assertEqual(result.failed_checks, [])
                self.assertEqual(result.executed_checks, ["statistical_outflows"])
                self.assertTrue(
                    any(f.evidence.get("transaction_id") == "sep_spike" for f in result.findings)
                )

    def test_absolute_detector_failure_does_not_remove_statistical_signal(self):
        def fail(context):
            raise RuntimeError("Synthetic detector failure")

        registry = DetectorRegistry(
            [
                replace(d, function=fail) if d.name == "large_outflows" else d
                for d in DetectorRegistry().detectors
            ]
        )
        data, profile = self.spike_data("20000.00")
        result = analyze_detailed(data, profile, [], registry=registry)
        self.assertEqual([f["check"] for f in result.failed_checks], ["large_outflows"])
        self.assertTrue(
            any(
                f.code == "UNUSUAL_TRANSACTION" and f.evidence.get("transaction_id") == "sep_spike"
                for f in result.findings
            )
        )

    def test_overlapping_outflow_signals_are_consolidated_once(self):
        data, profile = self.spike_data("20000.00")
        result = analyze_detailed(data, profile, [])
        merged = [f for f in result.findings if f.evidence.get("transaction_id") == "sep_spike"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].code, "LARGE_OUTFLOW")
        self.assertIn(
            "UNUSUAL_TRANSACTION", {f["code"] for f in merged[0].evidence["corroborating_signals"]}
        )

    def test_invalid_findings_are_quarantined_before_shared_processing(self):
        base = Finding("F-INVALID", "INVALID", "high", "Synthetic", "Synthetic finding", {})
        invalid_fields = [
            {"evidence": None},
            {"evidence": {"transaction_id": ["bad"]}},
            {"evidence": {"nested": {1: "bad"}}},
            {"evidence": {"amount": Decimal("NaN")}},
            {"evidence": {"amount": float("inf")}},
            {"evidence": {"value": object()}},
            {"severity": []},
            {"id": []},
            {"code": None},
            {"title": None},
            {"explanation": None},
        ]
        data, profile = self.spike_data("1800.00")
        healthy = analyze_detailed(data, profile, [])
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                bad = replace(base, **fields)
                registry = DetectorRegistry(
                    [
                        *DetectorRegistry().detectors,
                        Detector("invalid", lambda context, bad=bad: [base, bad]),
                    ]
                )
                result = analyze_detailed(data, profile, [], registry=registry)
                self.assertEqual(result.findings, healthy.findings)
                self.assertEqual([f["check"] for f in result.failed_checks], ["invalid"])
                self.assertNotIn("invalid", result.executed_checks)
                dumps(result)  # Serialization and evidence remain usable after the failure.

    def test_duplicate_finding_ids_cannot_replace_published_evidence(self):
        base = Finding("F-NEW", "NEW", "high", "Synthetic", "Synthetic finding", {})
        data, profile = self.spike_data("1800.00")
        healthy = analyze_detailed(data, profile, [])
        for findings in ([base, base], [replace(base, id="F-LOW_LIQUIDITY")]):
            with self.subTest(ids=[f.id for f in findings]):
                registry = DetectorRegistry(
                    [
                        *DetectorRegistry().detectors,
                        Detector("duplicates", lambda context, findings=findings: findings),
                    ]
                )
                result = analyze_detailed(data, profile, [], registry=registry)
                self.assertEqual(result.findings, healthy.findings)
                self.assertEqual([f["check"] for f in result.failed_checks], ["duplicates"])


class WorkflowBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "memory.sqlite3"

    def system(self, **kwargs):
        return build_system(db_path=self.db, crm_fixture=CRM, **kwargs)

    def test_analyzer_uses_one_request_and_registry_coverage(self):
        settings = Settings.from_dict(
            {"enabled_checks": ["liquidity", "statistical_outflows", "historical_comparison"]}
        )
        system, _ = self.system(settings=settings)
        report = system.run(SAMPLE)
        data = ClientData.parse(client_raw())
        profile = CRMProfile.parse(profile_raw(), data.client_id, data.as_of)
        direct = analyze_detailed(data, profile, [], selected=settings.enabled_checks)
        self.assertEqual(report["skipped_checks"], direct.skipped_checks)
        self.assertEqual(report["executed_checks"], direct.executed_checks)
        calls = [c["tool"] for c in report["tool_calls"] if c["agent"] == AnalyzerAgent.name]
        self.assertEqual(calls, ["analytics.scan"])

    def test_alternative_provider_and_call_only_agent_clients_complete_a_run(self):
        original, memory = self.system()
        provider = ScopedProvider(original.tools)
        self.assertIsInstance(provider, ToolProvider)
        report = OrchestratorAgent(provider, memory).run(SAMPLE)
        self.assertEqual(report["status"], "pending_review")
        self.assertEqual(report["summary"]["finding_count"], 8)
        self.assertFalse(report["advisory_release_allowed"])
        self.assertEqual(len(provider.observed), len(report["tool_calls"]))

    def test_call_only_client_is_rejected_at_the_provider_boundary(self):
        original, memory = self.system()
        with self.assertRaisesRegex(TypeError, "scoped agent clients"):
            OrchestratorAgent(CallOnlyClient(original.tools), memory)

    def test_report_and_actions_reuse_one_guard_decision(self):
        system, _ = self.system()
        with patch(
            "wealth_advisor.agents.evaluate_portfolio_guard",
            return_value=PortfolioGuard(False, ("synthetic_guard",)),
        ) as evaluate:
            report = system.run(SAMPLE)
        evaluate.assert_called_once()
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertEqual(report["guardrails"]["portfolio_blockers"], ["synthetic_guard"])
        action = next(a for a in report["actions"] if "CONCENTRATION" in a["id"])
        self.assertIn("before discussing any portfolio change", action["instruction"])

    def test_reused_dismissal_cannot_clear_guard_from_original_crm_evidence(self):
        profile = {**profile_raw(), "kyc_status": "pending"}
        fixture = self.root / "crm.json"
        fixture.write_text(dumps({profile["client_id"]: profile}), encoding="utf-8")
        system, memory = build_system(db_path=self.db, crm_fixture=fixture)
        first = system.run(SAMPLE)
        memory.review(
            first["run_id"],
            "approve",
            "Synthetic reviewer",
            "Synthetic test",
            dismissals={"F-KYC_REVIEW": "Synthetic prior annotation"},
        )
        later = {**client_raw(), "as_of": "2026-10-02"}
        path = self.root / "later.json"
        path.write_text(dumps(later), encoding="utf-8")
        report = system.run(path)
        self.assertNotIn("KYC_REVIEW", {f["code"] for f in report["findings"]})
        self.assertIn("finding:KYC_REVIEW", report["guardrails"]["portfolio_blockers"])
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertFalse(report["advisory_release_allowed"])
        action = next(a for a in report["actions"] if "CONCENTRATION" in a["id"])
        self.assertIn("before discussing any portfolio change", action["instruction"])

    def test_invalid_detector_output_preserves_reviewable_healthy_analysis(self):
        registry = DetectorRegistry(
            [
                *DetectorRegistry().detectors,
                Detector(
                    "invalid", lambda context: [Finding("F-BAD", "BAD", "high", "Bad", "Bad", None)]
                ),
            ]
        )
        system, _ = self.system(detector_registry=registry)
        report = system.run(SAMPLE)
        self.assertEqual(report["status"], "pending_review")
        self.assertEqual(report["execution_status"], "degraded")
        self.assertEqual(report["summary"]["finding_count"], 8)
        self.assertEqual([f["check"] for f in report["failed_checks"]], ["invalid"])
        self.assertIn("detector_failure", report["guardrails"]["portfolio_blockers"])
        self.assertFalse(report["advisory_release_allowed"])
