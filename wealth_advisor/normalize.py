"""Explicit lenient imports record every repair; financial snapshots are never defaulted."""

import copy
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .domain import ClientData, ValidationError, dumps, list_value, object_value


@dataclass
class PreparedInput:
    data: ClientData
    quality: dict[str, Any]


def prepare_input(raw: Any, mode: str = "strict") -> PreparedInput:
    if mode == "strict":
        data = ClientData.parse(raw)
        return PreparedInput(
            data,
            {
                "mode": mode,
                "score": Decimal("1.000"),
                "issues": [],
                "records_dropped": 0,
                "records_total": len(data.transactions) + len(data.holdings),
            },
        )
    if mode != "lenient":
        raise ValidationError("Unknown ingestion mode")
    raw = copy.deepcopy(object_value(raw, "client"))
    issues, dropped = [], 0

    def clean_money(item, key, location):
        value = item.get(key)
        if isinstance(value, str) and "," in value:
            # Only conventional 3-digit thousands grouping is accepted.
            import re

            if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", value):
                item[key] = value.replace(",", "")
                issues.append(
                    {"field": location, "action": "normalized", "reason": "thousands_separator"}
                )

    for key in ("cash_balance", "monthly_income", "monthly_expenses", "liabilities"):
        clean_money(raw, key, key)
    # Validate all snapshot fields and required arrays before considering partial records.
    transactions = list_value(raw.get("transactions"), "transactions")
    holdings = list_value(raw.get("holdings"), "holdings", maximum=10000)
    base = {**raw, "holdings": [], "transactions": []}
    ClientData.parse(base)
    normalized_holdings = []
    for index, holding in enumerate(holdings):
        holding = object_value(holding, "holding")
        clean_money(holding, "market_value", f"holdings[{index}].market_value")
        normalized_holdings.append(holding)
    # Missing/invalid holding values would understate the balance sheet, so remain fatal.
    ClientData.parse({**base, "holdings": normalized_holdings})
    normalized, seen = [], {}
    for index, item in enumerate(transactions):
        location = f"transactions[{index}]"
        if isinstance(item, dict):
            if item.get("currency", raw["currency"]) != raw["currency"]:
                raise ValidationError("mixed currencies require an FX adapter; normalize first")
            clean_money(item, "amount", location + ".amount")
            for key, default in (("category", "uncategorized"), ("counterparty", "unknown")):
                if item.get(key) is None or item.get(key) == "":
                    item[key] = default
                    issues.append(
                        {
                            "field": location + "." + key,
                            "action": "defaulted",
                            "reason": "missing_nonfinancial_label",
                        }
                    )
        try:
            parsed = ClientData.parse({**base, "transactions": [item]}).transactions[0]
        except ValidationError:
            dropped += 1
            issues.append({"field": location, "action": "dropped", "reason": "invalid_transaction"})
            continue
        signature = dumps(asdict(parsed), indent=None)
        if parsed.id in seen:
            if seen[parsed.id] != signature:
                raise ValidationError(
                    "Conflicting duplicate transaction IDs require manual reconciliation"
                )
            dropped += 1
            issues.append(
                {"field": location, "action": "deduplicated", "reason": "identical_record"}
            )
            continue
        seen[parsed.id] = signature
        normalized.append(item)
    data = ClientData.parse({**raw, "holdings": normalized_holdings, "transactions": normalized})
    total = len(transactions) + len(holdings)
    penalty = (Decimal(dropped) + Decimal(len(issues) - dropped) * Decimal("0.25")) / max(1, total)
    quality = {
        "mode": mode,
        "score": max(Decimal("0"), 1 - penalty).quantize(Decimal("0.001")),
        "issues": issues,
        "records_dropped": dropped,
        "records_total": total,
    }
    return PreparedInput(data, quality)
