"""Create contrasting synthetic clients, CRM profiles and runnable batch examples."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wealth_advisor.config import Settings  # noqa: E402
from wealth_advisor.domain import dumps  # noqa: E402
from wealth_advisor.observability import configure_logging  # noqa: E402
from wealth_advisor.runtime import build_system  # noqa: E402


def scenarios():
    base = json.loads((ROOT / "samples/client.json").read_text(encoding="utf-8"))
    base.update(
        client_id="balanced_demo", cash_balance="60000.00", liabilities="0.00", home_country="US"
    )
    base["holdings"] = [
        {"asset_id": "GLOBAL", "asset_class": "equity", "market_value": "30000.00"},
        {"asset_id": "DOMESTIC", "asset_class": "equity", "market_value": "30000.00"},
        {"asset_id": "BONDS", "asset_class": "bonds", "market_value": "40000.00"},
    ]
    base["transactions"] = []
    for month in (7, 8, 9):
        base["transactions"].append(
            {
                "id": f"salary_{month}",
                "date": f"2026-{month:02d}-01",
                "amount": "9000.00",
                "kind": "income",
                "category": "salary",
                "counterparty": "Synthetic employer",
            }
        )
        for day in range(2, 26, 3):
            base["transactions"].append(
                {
                    "id": f"groceries_{month}_{day}",
                    "date": f"2026-{month:02d}-{day:02d}",
                    "amount": "100.00",
                    "kind": "expense",
                    "category": "groceries",
                    "counterparty": "Synthetic market",
                    "country": "US",
                    "channel": "card",
                    "timestamp": f"2026-{month:02d}-{day:02d}T12:00:00-04:00",
                }
            )
    activity = copy.deepcopy(base)
    activity.update(client_id="activity_demo", cash_balance="6000.00")
    next(t for t in activity["transactions"] if t["id"] == "salary_9")["amount"] = "4500.00"
    for i in range(8):
        activity["transactions"].append(
            {
                "id": f"burst_{i}",
                "date": "2026-09-29",
                "amount": "2500.00",
                "kind": "expense",
                "category": "electronics",
                "counterparty": "Synthetic new merchant",
                "country": "SG",
                "channel": "card",
                "timestamp": f"2026-09-29T02:0{i}:00+08:00",
            }
        )
    for i in range(3):
        activity["transactions"].append(
            {
                "id": f"cash_{i}",
                "date": f"2026-09-{27 + i}",
                "amount": "9000.01",
                "kind": "withdrawal",
                "category": "cash",
                "counterparty": "Synthetic ATM",
                "country": "US",
                "channel": "cash",
                "timestamp": f"2026-09-{27 + i}T14:00:00-04:00",
            }
        )
    repairs = copy.deepcopy(base)
    repairs.update(client_id="import_demo", cash_balance="60,000.00")
    repairs["transactions"][1].pop("category")
    repairs["transactions"].append({"id": "malformed_import", "date": "bad"})
    repairs["transactions"].append(copy.deepcopy(repairs["transactions"][2]))
    return {"balanced": base, "activity": activity, "import_repairs": repairs}


def main():
    configure_logging("ERROR")
    clients = scenarios()
    target = ROOT / "samples/clients"
    target.mkdir(exist_ok=True)
    profile = json.loads((ROOT / "samples/crm.json").read_text(encoding="utf-8"))["client_demo_001"]
    crm = {}
    for name, raw in clients.items():
        (target / f"{name}.json").write_text(dumps(raw) + "\n", encoding="utf-8")
        crm[raw["client_id"]] = {**profile, "client_id": raw["client_id"]}
    fixture = ROOT / "samples/crm_batch.json"
    fixture.write_text(dumps(crm) + "\n", encoding="utf-8")
    settings = Settings.from_dict({"ingestion_mode": "lenient"})
    with tempfile.TemporaryDirectory() as directory:
        system, _ = build_system(
            db_path=Path(directory) / "batch.db", crm_fixture=fixture, settings=settings
        )
        reports = {name: system.run(target / f"{name}.json") for name in clients}
        (ROOT / "samples/batch_output.json").write_text(dumps(reports) + "\n", encoding="utf-8")
    print("Generated three synthetic clients and their lenient-mode reports.")


if __name__ == "__main__":
    main()
