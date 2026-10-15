"""Seeded offline corruption checks; unexpected internal failures fail the run."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import random
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wealth_advisor.config import Settings  # noqa: E402
from wealth_advisor.domain import dumps  # noqa: E402
from wealth_advisor.runtime import build_system  # noqa: E402


def corrupt(raw, rng):
    bad = [None, True, [], {}, "", "bad", "NaN", "Infinity", -1, "0.001", "1e9999"]
    operation = rng.randrange(12)
    if operation == 0:
        raw.pop(rng.choice(list(raw)))
    elif operation == 1:
        raw[rng.choice(list(raw))] = rng.choice(bad)
    elif operation == 2:
        tx = rng.choice(raw["transactions"])
        tx[rng.choice(list(tx))] = rng.choice(bad)
    elif operation == 3:
        raw["holdings"][0]["market_value"] = rng.choice(bad)
    elif operation == 4:
        raw["transactions"].append(copy.deepcopy(raw["transactions"][0]))
    elif operation == 5:
        raw["transactions"][0]["timestamp"] = rng.choice(
            [
                "2026-09-22T02:00:00",
                "2026-09-22T02:00:00+07:00",
                *bad,
            ]
        )
    elif operation == 6:
        raw["transactions"][0][rng.choice(["channel", "country", "currency"])] = rng.choice(bad)
    elif operation == 7:
        raw["cash_balance"] = rng.choice(["12,000.00", "1,20.00", "0", "999999999999999.99"])
    elif operation == 8:
        raw["transactions"] = [rng.choice(bad)]
    elif operation == 9:
        raw["transactions"][0].pop(rng.choice(["category", "counterparty", "amount"]))
    elif operation == 10:
        raw["monthly_expenses"] = "0"
        raw["transactions"] = []
    else:
        raw["as_of"] = rng.choice(["1900-01-01", "9999-12-31", "2026-02-30"])
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=800)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args()
    if not 1 <= args.cases <= 10000:
        parser.error("cases must be between 1 and 10000")
    logging.disable(logging.CRITICAL)
    rng, counts, errors = random.Random(args.seed), Counter(), []
    original = json.loads((ROOT / "samples/client.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        systems = {
            mode: build_system(
                db_path=root / f"{mode}.db",
                crm_fixture=ROOT / "samples/crm.json",
                settings=Settings.from_dict({"ingestion_mode": mode}),
            )[0]
            for mode in ("strict", "lenient")
        }
        for index in range(args.cases):
            mode = "strict" if index % 2 == 0 else "lenient"
            raw = corrupt(copy.deepcopy(original), rng)
            path = root / "corrupted.json"
            path.write_text(dumps(raw), encoding="utf-8")
            try:
                report = systems[mode].run(path)
                json.loads(dumps(report))
                assert not report["advisory_release_allowed"], "review gate bypassed"
                assert report["status"] in ("failed", "pending_review"), "invalid terminal state"
                if report["status"] == "failed":
                    assert report["error"]["code"] == "invalid_data", report["error"]
                    assert report["actions"] == [], "invalid input produced actions"
                    counts["expected_validation_failures"] += 1
                else:
                    assert not report["failed_checks"], report["failed_checks"]
                    assert report["memory_persisted"], "memory failed"
                    counts["completed"] += 1
                counts[mode] += 1
            except Exception as exc:
                errors.append({"case": index, "mode": mode, "error": str(exc)[:300]})
        # Corrupted requests must not poison later valid execution.
        for system in systems.values():
            report = system.run(ROOT / "samples/client.json")
            if report["status"] != "pending_review" or report["failed_checks"]:
                errors.append({"error": "healthy request failed after corruption"})
    print(
        json.dumps(
            {
                "passed": not errors,
                "seed": args.seed,
                "cases": args.cases,
                "counts": dict(counts),
                "unexpected_errors": errors,
            },
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
