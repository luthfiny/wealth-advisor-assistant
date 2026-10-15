"""Generate fresh demo artifacts with an isolated memory database."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wealth_advisor.cli import build_system, write_result  # noqa: E402
from wealth_advisor.observability import configure_logging  # noqa: E402


def main() -> None:
    configure_logging()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output = ROOT / "output" / ("demo-" + stamp)
    output.mkdir(parents=True)
    db = output / "memory.sqlite3"
    system, _ = build_system(db_path=db, crm_fixture=ROOT / "samples/crm.json")
    report = system.run(ROOT / "samples/client.json")
    write_result(report, output / "draft.json")

    outage_system, _ = build_system(
        db_path=db, crm_fixture=ROOT / "samples/crm.json", simulate_crm_failure=True
    )
    outage_report = outage_system.run(ROOT / "samples/client.json")
    (output / "crm-outage.json").write_text(
        json.dumps(outage_report, default=str, indent=2) + "\n",
        encoding="utf-8",
    )
    # This script never approves a run. The review command is a separate human action.
    print(f"\nArtifacts: {output}")
    print("Review the findings, then run:")
    print(
        f'python -m wealth_advisor review {report["run_id"]} --db "{db}" '
        '--decision approve --reviewer "Your name" --notes "Describe your validation"'
    )


if __name__ == "__main__":
    main()
