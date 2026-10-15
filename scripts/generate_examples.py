"""Regenerate checked-in examples from synthetic fixtures in temporary databases."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wealth_advisor.cli import build_system  # noqa: E402
from wealth_advisor.domain import dumps  # noqa: E402
from wealth_advisor.observability import configure_logging  # noqa: E402


def save(name: str, value: object) -> None:
    (ROOT / "samples" / name).write_text(dumps(value) + "\n", encoding="utf-8")


def main() -> None:
    configure_logging("ERROR")
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "examples.sqlite3"
        system, memory = build_system(db_path=db, crm_fixture=ROOT / "samples/crm.json")
        draft = system.run(ROOT / "samples/client.json")
        save("output.json", draft)
        action = next(a for a in draft["actions"] if "LARGE_OUTFLOW" in a["id"])
        # A synthetic review demonstrates the API; it is not a decision on actual client data.
        reviewed = memory.review(
            draft["run_id"],
            "approve",
            "Example reviewer",
            "Synthetic demonstration: examined the supplied fixture "
            "and retained investigation actions.",
            {
                action[
                    "id"
                ]: "Call the client to confirm withdrawal authorization and intended date."
            },
        )
        save("reviewed_output.json", reviewed)
        outage, _ = build_system(
            db_path=db, crm_fixture=ROOT / "samples/crm.json", simulate_crm_failure=True
        )
        save("crm_outage_output.json", outage.run(ROOT / "samples/client.json"))

        prior = json.loads((ROOT / "samples/client.json").read_text(encoding="utf-8"))
        prior["as_of"] = "2026-09-01"
        prior["cash_balance"] = "90000.00"
        prior["transactions"] = [t for t in prior["transactions"] if t["date"] <= prior["as_of"]]
        save("client_prior.json", prior)
        historical, _ = build_system(
            db_path=Path(directory) / "history.sqlite3", crm_fixture=ROOT / "samples/crm.json"
        )
        historical.run(ROOT / "samples/client_prior.json")
        save("historical_output.json", historical.run(ROOT / "samples/client.json"))
    print("Regenerated draft, reviewed, outage and historical examples from synthetic data.")


if __name__ == "__main__":
    main()
