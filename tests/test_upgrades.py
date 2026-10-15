import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

from tests.helpers import CRM, SAMPLE, client_raw, profile_raw
from wealth_advisor.analysis import Detector, DetectorRegistry, analyze_detailed
from wealth_advisor.cli import build_system, main
from wealth_advisor.config import CRMSettings, NarrativeSettings, Settings
from wealth_advisor.domain import ClientData, CRMProfile, ValidationError, dumps
from wealth_advisor.narrative import ClaudeNarrativeTool
from wealth_advisor.normalize import prepare_input
from wealth_advisor.policy import AnalysisPolicy
from wealth_advisor.tool_runtime import ToolError, ToolPolicy, ToolRegistry


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "memory.sqlite3"

    def system(self, settings=None, **kwargs):
        return build_system(db_path=self.db, crm_fixture=CRM, settings=settings, **kwargs)

    def input(self, raw=None, name="input.json"):
        path = self.root / name
        path.write_text(dumps(client_raw() if raw is None else raw), encoding="utf-8")
        return path

    def cli(self, arguments, stdin=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is None:
                code = main(arguments)
            else:
                with patch("builtins.input", side_effect=stdin):
                    code = main(arguments)
        return code, json.loads(stdout.getvalue())

    def test_catalog_and_explicit_skips(self):
        system, _ = self.system()
        report = system.run(SAMPLE)
        self.assertEqual(len(DetectorRegistry().catalog()), 18)
        skipped = {c["check"]: c["reason"] for c in report["skipped_checks"]}
        self.assertIn("timestamps", skipped["transaction_velocity"])
        self.assertIn("history", skipped["historical_comparison"])
        self.assertTrue(report["tool_calls"])
        self.assertEqual(report["narrative"]["source"], "template")
        for call in report["tool_calls"]:
            agent = next(a for a in system.agents if a.name == call["agent"])
            self.assertIn(call["tool"], agent.allowed_tools)
            self.assertGreaterEqual(call["duration_ms"], 0)

    def test_new_detectors_use_exact_amounts_and_recorded_timestamps(self):
        raw = client_raw()
        raw["home_country"] = "US"
        for tx in raw["transactions"]:
            tx.update(timestamp=tx["date"] + "T12:00:00+07:00", country="US", channel="card")
            if tx["id"] == "sep_salary":
                tx["amount"] = "4500.00"
        for index in range(8):
            raw["transactions"].append(
                {
                    "id": f"burst_{index}",
                    "date": "2026-09-29",
                    "amount": "2500.00",
                    "kind": "expense",
                    "category": "test",
                    "counterparty": "Synthetic merchant",
                    "timestamp": f"2026-09-29T02:0{index}:00+07:00",
                    "country": "SG",
                    "channel": "card",
                }
            )
        for index in range(3):
            raw["transactions"].append(
                {
                    "id": f"cash_{index}",
                    "date": f"2026-09-{27 + index}",
                    "amount": "9000.01",
                    "kind": "withdrawal",
                    "category": "cash",
                    "counterparty": "Synthetic ATM",
                    "timestamp": f"2026-09-{27 + index}T14:00:00+07:00",
                    "country": "US",
                    "channel": "cash",
                }
            )
        data = ClientData.parse(raw)
        profile = CRMProfile.parse(profile_raw(), data.client_id, data.as_of)
        result = analyze_detailed(data, profile, [])
        self.assertEqual(result.failed_checks, [])
        codes = {f.code for f in result.raw_findings}
        self.assertTrue(
            {
                "DUPLICATE_CHARGE",
                "TRANSACTION_VELOCITY",
                "ODD_HOURS",
                "NEW_COUNTRY",
                "STRUCTURING_PATTERN",
                "INCOME_DROP",
                "NEW_COUNTERPARTY",
            }
            <= codes
        )
        pattern = next(f for f in result.raw_findings if f.code == "STRUCTURING_PATTERN")
        self.assertEqual(pattern.evidence["total_amount"], Decimal("27000.03"))
        self.assertEqual(len(pattern.evidence["transaction_ids"]), 3)
        burst = next(f for f in result.raw_findings if f.code == "TRANSACTION_VELOCITY")
        self.assertEqual(burst.evidence["count"], 8)
        decline = next(f for f in result.findings if f.code == "INCOME_DROP")
        self.assertEqual(decline.evidence["decline_fraction"], Decimal("0.50"))
        merged = next(f for f in result.findings if f.evidence.get("transaction_id") == "burst_0")
        self.assertIn("corroborating_signals", merged.evidence)

    def test_invalid_detector_contract_and_duplicate_catalog(self):
        with self.assertRaises(ValidationError):
            DetectorRegistry([Detector("same", lambda c: []), Detector("same", lambda c: [])])
        result = analyze_detailed(
            ClientData.parse(client_raw()),
            None,
            [],
            registry=DetectorRegistry([Detector("invalid", lambda c: [42])]),
        )
        self.assertEqual(result.findings, [])
        self.assertEqual(result.failed_checks[0]["error_type"], "TypeError")

    def test_timestamp_requires_offset_and_matching_date(self):
        for timestamp in ("2026-09-22T02:00:00", "2026-09-21T02:00:00+07:00", "bad"):
            raw = client_raw()
            raw["transactions"][0]["timestamp"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValidationError):
                ClientData.parse(raw)

    def test_detector_crash_is_isolated_and_blocks_portfolio(self):
        def crash(context):
            raise RuntimeError("private input should not appear")

        registry = DetectorRegistry([*DetectorRegistry().detectors, Detector("broken", crash)])
        system, _ = self.system(detector_registry=registry)
        report = system.run(SAMPLE)
        self.assertEqual(report["execution_status"], "degraded")
        self.assertEqual(report["failed_checks"][0]["check"], "broken")
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertFalse(report["advisory_release_allowed"])
        self.assertGreater(len(report["findings"]), 0)
        self.assertNotIn("private input", dumps(report))

    def test_configured_subset_has_coverage_and_no_portfolio_clearance(self):
        settings = Settings.from_dict({"enabled_checks": ["liquidity"]})
        report = self.system(settings)[0].run(SAMPLE)
        self.assertEqual(report["executed_checks"], ["liquidity"])
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertTrue(
            any(c["reason"] == "disabled_by_configuration" for c in report["skipped_checks"])
        )

    def test_configuration_rejects_bad_types_and_relationships(self):
        invalid = [
            [],
            {"unknown": 1},
            {"analysis": {"typo": 1}},
            {"tools": {"max_attempts": True}},
            {"tools": {"deadline_seconds": "1"}},
            {"analysis": {"single_asset_limit": "1.1"}},
            {"analysis": {"minimum_baseline": 0}},
            {"analysis": {"critical_cash_months": "9"}},
            {"enabled_checks": ["liquidity", "liquidity"]},
            {"max_in_flight": 0},
            {"review": {"feedback_ttl_days": 0}},
            {"ingestion_mode": "guess"},
            {"analysis": {"emergency_months": "1e-999999"}},
            {"narrative": {"model": ""}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings.from_dict(value)

    def test_configuration_changes_actual_thresholds(self):
        settings = Settings.from_dict(
            {"analysis": {"emergency_months": "1", "critical_cash_months": "1"}}
        )
        report = self.system(settings)[0].run(SAMPLE)
        self.assertNotIn("LOW_LIQUIDITY", {f["code"] for f in report["findings"]})
        with self.assertRaises(ValidationError):
            self.system(Settings.from_dict({"enabled_checks": ["not-a-check"]}))

    def test_review_suppresses_later_identical_evidence_and_preserves_raw(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        fid = "F-LOW_LIQUIDITY"
        reviewed = memory.review(
            draft["run_id"],
            "approve",
            "Reviewer",
            "Planned reserve",
            dismissals={fid: "Client has a planned cash requirement"},
        )
        self.assertEqual(reviewed["findings"], json.loads(dumps(draft["findings"])))
        self.assertNotIn(fid, {f["id"] for f in reviewed["reviewed_findings"]})
        self.assertNotIn("A-LOW_LIQUIDITY", {a["id"] for a in reviewed["reviewed_actions"]})
        raw = client_raw()
        raw["as_of"] = "2026-10-02"
        later = system.run(self.input(raw))
        self.assertNotIn(fid, {f["id"] for f in later["findings"]})
        self.assertIn(fid, {f["id"] for f in later["raw_findings"]})
        self.assertEqual(later["suppressed_findings"][0]["feedback"]["reviewer"], "Reviewer")
        self.assertIn(fid, later["recurring_finding_ids"])
        self.assertFalse(later["advisory_release_allowed"])

    def test_changed_evidence_resurfaces_a_dismissal(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        memory.review(
            draft["run_id"],
            "approve",
            "Reviewer",
            "Checked",
            dismissals={"F-LOW_LIQUIDITY": "Reviewed"},
        )
        raw = client_raw()
        raw.update(as_of="2026-10-02", cash_balance="6000.00")
        later = system.run(self.input(raw))
        self.assertIn("F-LOW_LIQUIDITY", {f["id"] for f in later["findings"]})
        self.assertIn("evidence_changed", {e["outcome"] for e in later["feedback_events"]})

    def test_expiry_and_policy_change_resurface_dismissals(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        memory.review(
            draft["run_id"],
            "approve",
            "Reviewer",
            "Checked",
            dismissals={"F-LOW_LIQUIDITY": "Temporary"},
            ttl_days=1,
        )
        raw = client_raw()
        raw["as_of"] = "2026-10-03"
        later = system.run(self.input(raw))
        self.assertIn("expired", {e["outcome"] for e in later["feedback_events"]})
        changed = replace(
            Settings(), analysis=replace(AnalysisPolicy(), version="different-policy")
        )
        later = self.system(changed)[0].run(self.input(raw))
        self.assertIn("policy_changed", {e["outcome"] for e in later["feedback_events"]})

    def test_review_feedback_is_scoped_by_date_client_and_currency(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        memory.review(
            draft["run_id"],
            "approve",
            "Reviewer",
            "Checked",
            dismissals={"F-LOW_LIQUIDITY": "Reviewed"},
        )
        self.assertEqual(memory.feedback("client_demo_001", "USD", date(2026, 10, 1)), [])
        self.assertEqual(memory.feedback("other", "USD", date(2026, 10, 2)), [])
        self.assertEqual(memory.feedback("client_demo_001", "EUR", date(2026, 10, 2)), [])

    def test_severity_override_has_audited_raw_and_reviewed_versions(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        reviewed = memory.review(
            draft["run_id"],
            "approve",
            "Reviewer",
            "Validated planned need",
            severity_overrides={"F-LOW_LIQUIDITY": "info"},
        )
        self.assertEqual(
            next(f for f in reviewed["findings"] if f["id"] == "F-LOW_LIQUIDITY")["severity"],
            "high",
        )
        self.assertEqual(
            next(f for f in reviewed["reviewed_findings"] if f["id"] == "F-LOW_LIQUIDITY")[
                "severity"
            ],
            "info",
        )
        raw = client_raw()
        raw["as_of"] = "2026-10-02"
        later = system.run(self.input(raw))
        self.assertEqual(
            next(f for f in later["findings"] if f["id"] == "F-LOW_LIQUIDITY")["severity"], "info"
        )

    def test_invalid_annotation_cannot_partially_commit(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        for args in [
            {"dismissals": {"missing": "why"}},
            {"severity_overrides": {"F-LOW_LIQUIDITY": "bogus"}},
            {"dismissals": {"F-LOW_LIQUIDITY": ""}},
            {"decision": []},
        ]:
            supplied = {"decision": "approve", **args}
            with self.subTest(args=args), self.assertRaises(ValidationError):
                memory.review(draft["run_id"], reviewer="Reviewer", notes="Checked", **supplied)
        self.assertIsNone(memory.get(draft["run_id"])["review"])

    def test_crm_cache_is_explicit_and_does_not_clear_suitability(self):
        self.system()[0].run(SAMPLE)
        outage, _ = self.system(simulate_crm_failure=True)
        report = outage.run(SAMPLE)
        self.assertEqual(report["sources"]["crm_source"], "cache")
        self.assertTrue(report["sources"]["crm_available"])
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertEqual(report["execution_status"], "degraded")

    def test_crm_cache_excludes_future_observations(self):
        raw = client_raw()
        raw["as_of"] = "2026-10-20"
        self.system()[0].run(self.input(raw))
        report = self.system(simulate_crm_failure=True)[0].run(SAMPLE)
        self.assertEqual(report["sources"]["crm_source"], "unavailable")

    def test_crm_cache_respects_freshness_and_not_found(self):
        self.system()[0].run(SAMPLE)
        settings = replace(Settings(), crm=CRMSettings(cache_max_age_days=1))
        raw = client_raw()
        raw["as_of"] = "2026-10-03"
        report = self.system(settings, simulate_crm_failure=True)[0].run(self.input(raw))
        self.assertFalse(report["sources"]["crm_available"])
        empty = self.root / "crm.json"
        empty.write_text("{}")
        system, _ = build_system(db_path=self.db, crm_fixture=empty)
        self.assertFalse(system.run(SAMPLE)["sources"]["crm_available"])

    def test_batch_keeps_running_after_bad_input(self):
        bad = self.root / "bad.json"
        bad.write_text("invalid JSON")
        code, report = self.cli(
            [
                "batch",
                "--input",
                str(bad),
                str(SAMPLE),
                "--db",
                str(self.db),
                "--crm-fixture",
                str(CRM),
                "--output-dir",
                str(self.root / "reports"),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["reports"][1]["status"], "pending_review")
        self.assertEqual(len(list((self.root / "reports").glob("*.json"))), 2)

    def test_cli_configuration_and_catalogs(self):
        config = self.root / "settings.json"
        config.write_text('{"enabled_checks":["liquidity"]}')
        code, report = self.cli(
            [
                "--config",
                str(config),
                "analyze",
                "--input",
                str(SAMPLE),
                "--db",
                str(self.db),
                "--crm-fixture",
                str(CRM),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["executed_checks"], ["liquidity"])
        self.assertEqual(len(self.cli(["checks"])[1]["checks"]), 18)
        self.assertNotIn("crm.profile", self.cli(["tools"])[1]["agent_permissions"]["advisor"])

    def test_file_and_interactive_reviews(self):
        system, _ = self.system()
        draft = system.run(SAMPLE)
        decision = self.root / "decision.json"
        decision.write_text(
            json.dumps(
                {
                    "decision": "approve",
                    "reviewer": "File reviewer",
                    "notes": "Checked",
                    "dismissals": {"F-LOW_LIQUIDITY": "Planned expense"},
                }
            )
        )
        code, report = self.cli(
            ["review", draft["run_id"], "--db", str(self.db), "--decisions", str(decision)]
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["review"]["reviewer"], "File reviewer")
        draft = system.run(SAMPLE)
        code, report = self.cli(
            ["review", draft["run_id"], "--db", str(self.db), "--interactive"],
            stdin=["reject", "Interactive reviewer", "Check coverage"],
        )
        self.assertEqual(code, 0)
        self.assertFalse(report["advisory_release_allowed"])

    def test_bad_file_review_and_interactive_eof_leave_pending(self):
        system, memory = self.system()
        draft = system.run(SAMPLE)
        path = self.root / "decision.json"
        path.write_text('{"decision":[], "reviewer":"R", "notes":"x"}')
        self.assertEqual(
            self.cli(["review", draft["run_id"], "--db", str(self.db), "--decisions", str(path)])[
                0
            ],
            2,
        )
        self.assertEqual(
            self.cli(
                ["review", draft["run_id"], "--db", str(self.db), "--interactive"], stdin=EOFError()
            )[0],
            2,
        )
        self.assertIsNone(memory.get(draft["run_id"])["review"])

    def test_lenient_mode_records_repairs_without_guessing_balances(self):
        raw = client_raw()
        raw["cash_balance"] = "12,000.00"
        raw["transactions"][0].pop("category")
        raw["transactions"].append({"id": "bad", "date": "bad"})
        raw["transactions"].append(raw["transactions"][1].copy())
        prepared = prepare_input(raw, "lenient")
        self.assertEqual(prepared.data.cash_balance, Decimal("12000.00"))
        self.assertEqual(prepared.quality["records_dropped"], 2)
        self.assertLess(prepared.quality["score"], 1)
        report = self.system(replace(Settings(), ingestion_mode="lenient"))[0].run(self.input(raw))
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertEqual(report["execution_status"], "degraded")
        del raw["cash_balance"]
        with self.assertRaises(ValidationError):
            prepare_input(raw, "lenient")

    def test_lenient_conflicting_duplicates_are_fatal(self):
        raw = client_raw()
        changed = raw["transactions"][0].copy()
        changed["amount"] = "1"
        raw["transactions"].append(changed)
        with self.assertRaises(ValidationError):
            prepare_input(raw, "lenient")

    def test_optional_llm_unavailable_falls_back_without_bypassing_review(self):
        settings = replace(Settings(), narrative=NarrativeSettings(enabled=True))
        with patch.dict("os.environ", {}, clear=True):
            report = self.system(settings)[0].run(SAMPLE)
        self.assertEqual(report["narrative"]["source"], "template")
        self.assertFalse(report["advisory_release_allowed"])
        self.assertEqual(report["execution_status"], "degraded")

    def test_claude_only_selects_existing_local_sentences(self):
        draft = self.system()[0].run(SAMPLE)
        brief = {
            "metrics": draft["metrics"],
            "findings": draft["findings"],
            "actions": draft["actions"],
        }
        fid = draft["findings"][0]["id"]
        for ids, valid in [([fid], True), (["invented"], False), ([fid, fid], False)]:
            selection = json.dumps({"sentence_ids": ids})
            response = Mock()
            response.read1.side_effect = [
                json.dumps(
                    {"stop_reason": "end_turn", "content": [{"type": "text", "text": selection}]}
                ).encode(),
                b"",
            ]
            response.fp = None
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            tool = ClaudeNarrativeTool(NarrativeSettings(), opener=opener)
            with (
                self.subTest(ids=ids),
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-only-placeholder"}),
            ):
                if valid:
                    self.assertEqual(tool.execute({"brief": brief})["source"], "claude_selection")
                else:
                    with self.assertRaises(ToolError):
                        tool.execute({"brief": brief})
            request = opener.open.call_args.args[0]
            sent = request.data.decode()
            self.assertNotIn("182000", sent)
            self.assertNotIn("Demo Recipient", sent)
            self.assertNotIn("client_demo_001", sent)

    def test_claude_invalid_responses_use_template_without_releasing_draft(self):
        settings = replace(Settings(), narrative=NarrativeSettings(enabled=True))
        invalid_payloads = [
            b"bad JSON",
            b"[]",
            b"x" * 65537,
            json.dumps({"stop_reason": "max_tokens", "content": []}).encode(),
            json.dumps(
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": '{"sentence_ids":[],"extra":1}'}],
                }
            ).encode(),
        ]
        for payload in invalid_payloads:
            opener, response = MagicMock(), Mock()
            response.fp = None
            response.read1.side_effect = [payload, b""]
            opener.open.return_value.__enter__.return_value = response
            system, _ = self.system(settings)
            system.tools._tools["narrative.llm"] = ClaudeNarrativeTool(settings.narrative, opener)
            with (
                self.subTest(payload=payload[:50]),
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-only-placeholder"}),
            ):
                report = system.run(SAMPLE)
            self.assertEqual(report["narrative"]["source"], "template")
            self.assertEqual(report["execution_status"], "degraded")
            self.assertFalse(report["advisory_release_allowed"])

    def test_claude_transport_errors_are_sanitized(self):
        for error in [
            HTTPError("https://api.anthropic.com", 429, "private message", {}, io.BytesIO()),
            URLError("private transport message"),
        ]:
            opener = Mock()
            opener.open.side_effect = error
            tool = ClaudeNarrativeTool(NarrativeSettings(), opener)
            with (
                self.subTest(error=type(error).__name__),
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-only-placeholder"}),
            ):
                with self.assertRaises(ToolError) as caught:
                    tool.execute({"brief": {"findings": []}})
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(caught.exception.retryable)


class ToolCapabilityTests(unittest.TestCase):
    def test_scope_prevents_calls_and_records_safe_metadata(self):
        tool, observed = Mock(), []
        tool.execute.return_value = "ok"
        registry = ToolRegistry({"allowed": tool, "forbidden": tool})
        scoped = registry.scoped({"allowed"}, observed.append)
        with self.assertRaises(ToolError):
            scoped.call("forbidden", {}, run_id="r")
        self.assertEqual(tool.execute.call_count, 0)
        result = scoped.invoke("allowed", {"sensitive": "hidden"}, run_id="r")
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertNotIn("hidden", dumps(observed))

    def test_structured_denials_do_not_execute_or_retry(self):
        tool = Mock()
        registry = ToolRegistry({"registered": tool})
        scope = registry.scoped(set())
        denied = scope.invoke("registered", {}, run_id="r")
        unknown = registry.invoke("missing", {}, run_id="r")
        for result in (denied, unknown):
            self.assertFalse(result.ok)
            self.assertEqual(result.error.kind, "forbidden")
            self.assertEqual(result.attempts, 0)
        tool.execute.assert_not_called()

    def test_capacity_contention_does_not_reset_backend_failures(self):
        now = [0.0]
        tool = Mock()
        tool.execute.side_effect = ToolError("down", "unavailable", retryable=True)
        policy = ToolPolicy(max_attempts=1, circuit_threshold=2, circuit_cooldown_seconds=5)
        registry = ToolRegistry(
            {"api": tool}, policies={"api": policy}, max_in_flight=1, clock=lambda: now[0]
        )
        self.assertFalse(registry.invoke("api", {}, run_id="r").ok)
        self.assertTrue(registry._slots.acquire(blocking=False))
        try:
            self.assertEqual(registry.invoke("api", {}, run_id="r").error.code, "tool_busy")
        finally:
            registry._slots.release()
        self.assertFalse(registry.invoke("api", {}, run_id="r").ok)
        self.assertEqual(registry.invoke("api", {}, run_id="r").error.code, "circuit_open")
        self.assertEqual(tool.execute.call_count, 2)

    def test_circuit_counts_calls_and_recovers_with_single_probe(self):
        now = [0.0]
        tool = Mock()
        tool.execute.side_effect = ToolError("unavailable", "down", retryable=True)
        policy = ToolPolicy(max_attempts=1, circuit_threshold=2, circuit_cooldown_seconds=5)
        registry = ToolRegistry({"api": tool}, policies={"api": policy}, clock=lambda: now[0])
        for _ in range(2):
            self.assertFalse(registry.invoke("api", {}, run_id="r").ok)
        blocked = registry.invoke("api", {}, run_id="r")
        self.assertEqual(blocked.error.code, "circuit_open")
        self.assertEqual(blocked.attempts, 0)
        self.assertEqual(tool.execute.call_count, 2)
        now[0] += 5
        tool.execute.side_effect = None
        tool.execute.return_value = "healthy"
        self.assertEqual(registry.call("api", {}, run_id="r"), "healthy")
        self.assertEqual(registry.call("api", {}, run_id="r"), "healthy")

    def test_not_found_does_not_open_circuit(self):
        tool = Mock()
        tool.execute.side_effect = ToolError("crm_not_found", "missing")
        registry = ToolRegistry({"api": tool}, policies={"api": ToolPolicy(circuit_threshold=1)})
        for _ in range(3):
            self.assertEqual(registry.invoke("api", {}, run_id="r").error.kind, "not_found")
        self.assertEqual(tool.execute.call_count, 3)

    def test_deadline_bounds_retries(self):
        now = [0.0]
        tool = Mock()
        tool.execute.side_effect = ToolError("down", "down", retryable=True)
        policy = ToolPolicy(max_attempts=10, backoff_seconds=1, deadline_seconds=1)
        registry = ToolRegistry(
            {"api": tool},
            policies={"api": policy},
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        result = registry.invoke("api", {}, run_id="r")
        self.assertEqual(result.error.code, "tool_deadline")
        self.assertEqual(tool.execute.call_count, 1)

    def test_concurrency_is_bounded_without_detached_workers(self):
        started, release = threading.Event(), threading.Event()

        class SlowTool:
            def execute(self, arguments):
                started.set()
                release.wait(timeout=2)
                return "ok"

        registry = ToolRegistry({"api": SlowTool()}, max_attempts=1, max_in_flight=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(registry.call, "api", {}, run_id="r")
            self.assertTrue(started.wait(timeout=1))
            try:
                self.assertEqual(registry.invoke("api", {}, run_id="r").error.code, "tool_busy")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=1), "ok")
