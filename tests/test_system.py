import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock

from tests.helpers import CRM, SAMPLE, client_raw
from tests.test_tools import running_crm
from wealth_advisor.agents import AdvisorAgent, AnalyzerAgent, DataFetcherAgent
from wealth_advisor.cli import build_system, main
from wealth_advisor.domain import ValidationError
from wealth_advisor.memory import MemoryError, SQLiteMemory
from wealth_advisor.observability import configure_logging


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db = self.directory / "memory.sqlite3"
        self.system, self.memory = build_system(db_path=self.db, crm_fixture=CRM)

    def input_file(self, raw):
        path = self.directory / "input.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_complete_flow_waits_for_review_and_references_real_findings(self):
        report = self.system.run(SAMPLE)
        self.assertEqual(report["status"], "pending_review")
        self.assertEqual(report["execution_status"], "completed")
        self.assertFalse(report["advisory_release_allowed"])
        self.assertEqual(report["reviewed_actions"], [])
        self.assertEqual(
            [e["agent"] for e in report["events"]], ["data_fetcher", "analyzer", "advisor"]
        )
        finding_ids = {f["id"] for f in report["findings"]}
        for action in report["actions"]:
            self.assertTrue(set(action["finding_ids"]) <= finding_ids)
            self.assertTrue(action["requires_human_review"])
        self.assertEqual(self.memory.get(report["run_id"])["run_id"], report["run_id"])

    def test_dependency_routing_works_with_unordered_agents(self):
        self.system.agents = [AdvisorAgent(), AnalyzerAgent(), DataFetcherAgent()]
        self.assertEqual(self.system.run(SAMPLE)["execution_status"], "completed")

    def test_invalid_plan_fails_closed(self):
        self.system.agents = [AnalyzerAgent()]
        report = self.system.run(SAMPLE)
        self.assertEqual(report["error"]["code"], "invalid_agent_plan")
        self.assertFalse(report["advisory_release_allowed"])

    def test_unexpected_agent_failure_produces_no_partial_actions(self):
        bad = Mock(name="failing_agent")
        bad.name, bad.requires, bad.provides = "bad", frozenset(), frozenset()
        bad.run.side_effect = RuntimeError("private source data")
        self.system.agents = [bad]
        report = self.system.run(SAMPLE)
        self.assertEqual(report["error"]["code"], "agent_internal_error")
        self.assertNotIn("private", json.dumps(report))
        self.assertEqual(report["actions"], [])

    def test_crm_outage_produces_limited_draft_with_blocked_suitability(self):
        system, _ = build_system(db_path=self.db, crm_fixture=CRM, simulate_crm_failure=True)
        report = system.run(SAMPLE)
        self.assertEqual(report["execution_status"], "degraded")
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertIsNone(report["sources"]["crm_profile"])
        self.assertIn("CRM_UNAVAILABLE", {f["code"] for f in report["findings"]})
        concentration = next(a for a in report["actions"] if "CONCENTRATION" in a["id"])
        self.assertIn("refresh CRM", concentration["instruction"])

    def test_real_http_crm_integrates_with_orchestrator(self):
        with running_crm() as url:
            system, _ = build_system(db_path=self.db, crm_url=url)
            report = system.run(SAMPLE)
        self.assertEqual(report["execution_status"], "completed")
        self.assertTrue(report["sources"]["crm_available"])

    def test_missing_input_is_a_structured_failure(self):
        report = self.system.run(self.directory / "does_not_exist.json")
        self.assertEqual(report["error"]["code"], "input_unreadable")
        self.assertEqual(report["status"], "failed")
        self.assertFalse(self.db.exists())

    def test_invalid_json_and_missing_data_do_not_crash(self):
        invalid = self.directory / "bad.json"
        invalid.write_text("not json", encoding="utf-8")
        self.assertEqual(self.system.run(invalid)["status"], "failed")
        report = self.system.run(self.input_file({"client_id": "x"}))
        self.assertEqual(report["error"]["code"], "invalid_data")

    def test_broken_storage_degrades_and_blocks_review(self):
        blocker = self.directory / "file"
        blocker.write_text("not a directory", encoding="utf-8")
        system, _ = build_system(db_path=blocker / "memory.sqlite3", crm_fixture=CRM)
        report = system.run(SAMPLE)
        self.assertEqual(report["execution_status"], "degraded")
        self.assertFalse(report["memory_persisted"])
        self.assertFalse(report["advisory_release_allowed"])
        self.assertGreater(len(report["findings"]), 0)

    def test_memory_excludes_same_date_future_and_other_currencies(self):
        self.system.run(SAMPLE)
        self.assertEqual(self.memory.history("client_demo_001", "USD", "2026-10-01"), [])
        self.assertEqual(self.memory.history("client_demo_001", "EUR", "2026-11-01"), [])
        self.assertEqual(self.memory.history("other_client", "USD", "2026-11-01"), [])
        self.assertEqual(len(self.memory.history("client_demo_001", "USD", "2026-11-01")), 1)

    def test_separate_runs_reuse_history_and_sessions_do_not_leak(self):
        raw = client_raw()
        raw["cash_balance"] = "90000"
        prior = self.system.run(self.input_file(raw))
        raw["as_of"] = "2026-10-02"
        raw["cash_balance"] = "12000"
        current = self.system.run(self.input_file(raw))
        self.assertEqual(current["sources"]["history_run_ids"], [prior["run_id"]])
        self.assertIn("NET_WORTH_DROP", {f["code"] for f in current["findings"]})
        self.assertEqual(len(current["events"]), 3)

    def test_approve_preserves_original_and_audits_override(self):
        report = self.system.run(SAMPLE)
        action = report["actions"][0]
        approved = self.memory.review(
            report["run_id"],
            "approve",
            "Reviewer",
            "Checked records",
            {action["id"]: "Call client to validate the cash requirement."},
        )
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["advisory_release_allowed"])
        self.assertEqual(approved["actions"][0]["instruction"], action["instruction"])
        self.assertIn("Call client", approved["reviewed_actions"][0]["instruction"])
        self.assertEqual(self.memory.get(report["run_id"])["review"], approved["review"])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            original = json.loads(connection.execute("SELECT report_json FROM runs").fetchone()[0])
        self.assertEqual(original["status"], "pending_review")
        self.assertIsNone(original["review"])

    def test_rejection_keeps_release_blocked_and_is_excluded_from_history(self):
        report = self.system.run(SAMPLE)
        rejected = self.memory.review(report["run_id"], "reject", "Reviewer", "Bad source coverage")
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(rejected["advisory_release_allowed"])
        self.assertEqual(rejected["reviewed_actions"], [])
        self.assertEqual(self.memory.history("client_demo_001", "USD", "2026-11-01"), [])

    def test_duplicate_or_invalid_reviews_do_not_overwrite_decision(self):
        report = self.system.run(SAMPLE)
        with self.assertRaises(ValidationError):
            self.memory.review(report["run_id"], "approve", "Reviewer", "", {})
        with self.assertRaises(ValidationError):
            self.memory.review(report["run_id"], "approve", "Reviewer", "Checked", {"fake": "x"})
        self.memory.review(report["run_id"], "reject", "Reviewer", "Needs corrections")
        with self.assertRaises(ValidationError):
            self.memory.review(report["run_id"], "approve", "Someone else", "Ignore rejection")
        self.assertEqual(self.memory.get(report["run_id"])["status"], "rejected")

    def test_concurrent_review_has_exactly_one_winner(self):
        report = self.system.run(SAMPLE)

        def attempt(name):
            try:
                SQLiteMemory(self.db).review(report["run_id"], "approve", name, "Checked")
                return "ok"
            except ValidationError:
                return "already_reviewed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["Reviewer A", "Reviewer B"]))
        self.assertCountEqual(results, ["ok", "already_reviewed"])

    def test_unknown_run_returns_validation_error(self):
        with self.assertRaises(ValidationError):
            self.memory.get("absent")
        with self.assertRaises(ValidationError):
            self.memory.review("absent", "approve", "Reviewer", "Checked")

    def test_corrupt_historical_metric_degrades_without_losing_current_analysis(self):
        report = self.system.run(SAMPLE)
        saved = self.memory.get(report["run_id"])
        saved["metrics"]["net_worth"] = "invalid"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE runs SET report_json = ?", (json.dumps(saved),))
        raw = client_raw()
        raw["as_of"] = "2026-10-02"
        current = self.system.run(self.input_file(raw))
        self.assertEqual(current["execution_status"], "degraded")
        self.assertEqual(current["sources"]["history_run_ids"], [])
        self.assertEqual(current["metrics"]["net_worth"], 182000)

    def test_corrupt_saved_report_fails_review_without_recording_decision(self):
        report = self.system.run(SAMPLE)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE runs SET report_json = ?", ("not json",))
        with self.assertRaises(MemoryError):
            self.memory.review(report["run_id"], "approve", "Reviewer", "Checked")
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM reviews").fetchone()[0], 0)

    def test_cli_json_stdout_logs_stderr_and_failure_exit_code(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "analyze",
                    "--input",
                    str(SAMPLE),
                    "--crm-fixture",
                    str(CRM),
                    "--db",
                    str(self.db),
                    "--output",
                    str(self.directory / "out.json"),
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "pending_review")
        for line in stderr.getvalue().splitlines():
            event = json.loads(line)
            self.assertIn("event", event)
            self.assertNotIn("Demo Market", line)
            self.assertNotIn("client_demo_001", line)
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(["analyze", "--input", str(self.directory / "missing.json")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "failed")
        configure_logging("ERROR")

    def test_cli_review_and_inspect_share_the_persisted_decision(self):
        draft = self.system.run(SAMPLE)
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "review",
                    draft["run_id"],
                    "--db",
                    str(self.db),
                    "--decision",
                    "approve",
                    "--reviewer",
                    "CLI reviewer",
                    "--notes",
                    "Verified synthetic records",
                ]
            )
        self.assertEqual(code, 0)
        approved = json.loads(stdout.getvalue())
        self.assertTrue(approved["advisory_release_allowed"])
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(["inspect", draft["run_id"], "--db", str(self.db)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), approved)
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "review",
                    draft["run_id"],
                    "--db",
                    str(self.db),
                    "--decision",
                    "reject",
                    "--reviewer",
                    "Second reviewer",
                    "--notes",
                    "Concurrent change",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "command_error")
        configure_logging("ERROR")


if __name__ == "__main__":
    unittest.main()
