import unittest
from datetime import date
from decimal import Decimal

from tests.helpers import client_raw, profile_raw
from wealth_advisor.domain import ClientData, CRMProfile, ValidationError, dumps


class DomainTests(unittest.TestCase):
    def test_money_remains_exact_in_json(self):
        raw = client_raw()
        raw["cash_balance"] = "0.10"
        data = ClientData.parse(raw)
        self.assertEqual(data.cash_balance + Decimal("0.20"), Decimal("0.30"))
        self.assertIn('"0.10"', dumps({"cash": data.cash_balance}))

    def test_invalid_money_is_rejected(self):
        for value in [None, True, -1, "NaN", "Infinity", "1.001", "1e100", {}, []]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                raw = client_raw()
                raw["cash_balance"] = value
                ClientData.parse(raw)

    def test_missing_required_field_is_rejected(self):
        for key in ["client_id", "as_of", "history_start", "currency", "transactions", "holdings"]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                raw = client_raw()
                del raw[key]
                ClientData.parse(raw)

    def test_duplicate_ids_rejected(self):
        for key in ["transactions", "holdings"]:
            raw = client_raw()
            raw[key].append(raw[key][0].copy())
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ClientData.parse(raw)

    def test_mixed_currencies_rejected(self):
        for key in ["transactions", "holdings"]:
            raw = client_raw()
            raw[key][0]["currency"] = "EUR"
            with self.subTest(key=key), self.assertRaises(ValidationError):
                ClientData.parse(raw)

    def test_dates_must_be_within_history(self):
        for value in ["2026-10-02", "2026-06-30", "2026-02-30", "2026-9-01", None]:
            raw = client_raw()
            raw["transactions"][0]["date"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ClientData.parse(raw)

    def test_invalid_enum_types_are_validation_errors(self):
        raw = client_raw()
        raw["transactions"][0]["kind"] = []
        with self.assertRaises(ValidationError):
            ClientData.parse(raw)
        raw = client_raw()
        raw["holdings"][0]["asset_class"] = {}
        with self.assertRaises(ValidationError):
            ClientData.parse(raw)

    def test_empty_history_and_holdings_are_valid(self):
        raw = client_raw()
        raw["transactions"] = []
        raw["holdings"] = []
        self.assertEqual(ClientData.parse(raw).holdings, ())

    def test_profile_identity_future_date_and_bad_horizon_rejected(self):
        for key, value in [
            ("client_id", "other"),
            ("updated_at", "2026-10-02"),
            ("investment_horizon_years", True),
            ("risk_tolerance", []),
            ("kyc_status", "unknown"),
        ]:
            raw = profile_raw()
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                CRMProfile.parse(raw, "client_demo_001", date(2026, 10, 1))


if __name__ == "__main__":
    unittest.main()
