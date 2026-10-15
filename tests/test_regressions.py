"""Boundary regressions found while validating the submission independently."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from tests.helpers import CRM, SAMPLE, client_raw
from wealth_advisor.cli import build_system, main
from wealth_advisor.domain import decimal_value, dumps
from wealth_advisor.tools import FinancialDataTool, ToolError, read_json


class BoundaryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_json_numeric_money_keeps_every_cent(self):
        payload = json.dumps(client_raw()).replace('"12000.00"', "999999999999999.99")
        path = self.root / "numeric.json"
        path.write_text(payload, encoding="utf-8")
        data = FinancialDataTool().execute({"path": path})
        self.assertEqual(data.cash_balance, Decimal("999999999999999.99"))

    def test_json_numeric_subcent_amount_is_rejected_without_float_rounding(self):
        payload = json.dumps(client_raw()).replace('"12000.00"', "999999999999999.991")
        path = self.root / "subcent.json"
        path.write_text(payload, encoding="utf-8")
        system, _ = build_system(db_path=self.root / "memory.sqlite3", crm_fixture=CRM)
        report = system.run(path)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["code"], "invalid_data")

    def test_zero_amount_with_extreme_exponent_has_bounded_serialization(self):
        amount = decimal_value("0e-10000", "amount")
        self.assertLess(len(dumps({"amount": amount})), 50)

    def test_duplicate_json_fields_are_rejected_instead_of_overwritten(self):
        path = self.root / "duplicate.json"
        path.write_text('{"cash_balance": "100", "cash_balance": "999"}', encoding="utf-8")
        with self.assertRaises(ToolError):
            read_json(path)

    def test_output_cannot_replace_inputs_or_database(self):
        input_path = self.root / "client.json"
        input_path.write_bytes(SAMPLE.read_bytes())
        fixture = self.root / "crm.json"
        fixture.write_bytes(CRM.read_bytes())
        database = self.root / "memory.sqlite3"
        system, _ = build_system(db_path=database, crm_fixture=fixture)
        system.run(input_path)
        for target in [input_path, fixture, database]:
            original = target.read_bytes()
            with self.subTest(target=target.name):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = main(
                        [
                            "analyze",
                            "--input",
                            str(input_path),
                            "--crm-fixture",
                            str(fixture),
                            "--db",
                            str(database),
                            "--output",
                            str(target),
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertEqual(target.read_bytes(), original)

    def test_output_hardlink_to_input_is_rejected(self):
        input_path = self.root / "client.json"
        input_path.write_bytes(SAMPLE.read_bytes())
        output = self.root / "alias.json"
        try:
            os.link(input_path, output)
        except OSError:
            self.skipTest("Hard links are unavailable on this filesystem")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "analyze",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output),
                    "--db",
                    str(self.root / "memory.sqlite3"),
                    "--crm-fixture",
                    str(CRM),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(input_path.read_bytes(), SAMPLE.read_bytes())

    def test_review_output_collision_does_not_finalize_the_decision(self):
        database = self.root / "memory.sqlite3"
        system, memory = build_system(db_path=database, crm_fixture=CRM)
        draft = system.run(SAMPLE)
        original = database.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "review",
                    draft["run_id"],
                    "--db",
                    str(database),
                    "--output",
                    str(database),
                    "--decision",
                    "approve",
                    "--reviewer",
                    "Synthetic reviewer",
                    "--notes",
                    "Test",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(memory.get(draft["run_id"])["status"], "pending_review")

    def test_checked_in_sample_matches_fresh_financial_results(self):
        system, _ = build_system(db_path=self.root / "memory.sqlite3", crm_fixture=CRM)
        actual = json.loads(dumps(system.run(SAMPLE)))
        expected = json.loads((SAMPLE.parent / "output.json").read_text(encoding="utf-8"))
        for field in ["metrics", "findings", "actions", "summary", "guardrails"]:
            with self.subTest(field=field):
                self.assertEqual(actual[field], expected[field])
