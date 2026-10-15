"""Exercise real CLI processes and HTTP CRM servers against synthetic data only."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/client.json"
CRM = ROOT / "samples/crm.json"


class ValidationFailed(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailed(message)


class Runner:
    def __init__(self, output: Path):
        self.output = output
        self.results: list[dict] = []
        self.environment = {**os.environ, "PYTHONUTF8": "1"}
        self.environment.pop("ANTHROPIC_API_KEY", None)

    def run(self, name: str, *arguments: str, expected_exit: int = 0) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "wealth_advisor", *arguments],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        (self.output / f"{name}.json").write_text(result.stdout, encoding="utf-8")
        (self.output / f"{name}.jsonl").write_text(result.stderr, encoding="utf-8")
        require(result.returncode == expected_exit, f"{name}: unexpected exit {result.returncode}")
        report = json.loads(result.stdout)
        events = [json.loads(line) for line in result.stderr.splitlines()]
        require(all("event" in event for event in events), f"{name}: invalid structured logs")
        require("client_demo_001" not in result.stderr, f"{name}: client ID leaked into logs")
        self.results.append(
            {
                "name": name,
                "exit_code": result.returncode,
                "status": report.get("status", "catalog"),
                "passed": True,
            }
        )
        return report

    @contextmanager
    def server(self, mode: str):
        log_path = self.output / f"crm-{mode}.jsonl"
        with log_path.open("w", encoding="utf-8") as log:
            with subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "wealth_advisor",
                    "serve-crm",
                    "--port",
                    "0",
                    "--fixture",
                    str(CRM),
                    "--failure-mode",
                    mode,
                ],
                cwd=ROOT,
                env=self.environment,
                stdout=subprocess.DEVNULL,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        content = log_path.read_text(encoding="utf-8")
                        if "\n" in content:
                            event = json.loads(content.splitlines()[0])
                            require(event.get("event") == "mock_crm.started", "CRM failed to start")
                            url = f"http://127.0.0.1:{event['port']}"
                            break
                        require(process.poll() is None, "CRM exited before becoming ready")
                        time.sleep(0.01)
                    else:
                        raise ValidationFailed("CRM startup timed out")
                    with urlopen(url + "/health", timeout=2) as response:
                        require(json.load(response) == {"status": "ok"}, "CRM health failed")
                    yield url
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def validate(runner: Runner) -> None:
    database = str(runner.output / "memory.sqlite3")
    common = ["--input", str(SAMPLE), "--db", database, "--crm-fixture", str(CRM)]
    export = runner.output / "draft.json"
    draft = runner.run("draft", "analyze", *common, "--output", str(export))
    require(
        draft["status"] == "pending_review" and not draft["advisory_release_allowed"],
        "Draft bypassed human review",
    )
    require(draft["metrics"]["net_worth"] == "182000.00", "Net worth calculation is incorrect")
    require(draft["summary"]["finding_count"] == 8, "Unexpected sample findings")
    require(json.loads(export.read_text(encoding="utf-8")) == draft, "Export differs from stdout")

    action = next(a for a in draft["actions"] if "LARGE_OUTFLOW" in a["id"])
    review = [
        draft["run_id"],
        "--db",
        database,
        "--reviewer",
        "Synthetic validation reviewer",
        "--notes",
        "Automated test of fictional data; no real client decision.",
    ]
    approved = runner.run(
        "approve",
        "review",
        *review,
        "--decision",
        "approve",
        "--override",
        f"{action['id']}=Confirm authorization with the fictional client.",
    )
    require(
        approved["status"] == "approved" and approved["advisory_release_allowed"],
        "Approval did not release reviewed actions",
    )
    require(approved["actions"] == draft["actions"], "Approval changed the original draft")
    require(approved["findings"] == draft["findings"], "Review changed financial evidence")
    edited = next(a for a in approved["reviewed_actions"] if a["id"] == action["id"])
    require(
        edited["instruction"] == "Confirm authorization with the fictional client.",
        "Human action override was lost",
    )
    inspected = runner.run("inspect", "inspect", draft["run_id"], "--db", database)
    require(inspected == approved, "Review did not persist across processes")
    runner.run("duplicate-review", "review", *review, "--decision", "reject", expected_exit=2)

    rejected_draft = runner.run("reject-draft", "analyze", *common)
    rejected = runner.run(
        "reject",
        "review",
        rejected_draft["run_id"],
        "--db",
        database,
        "--decision",
        "reject",
        "--reviewer",
        "Synthetic validation reviewer",
        "--notes",
        "Synthetic rejection path.",
    )
    require(
        not rejected["advisory_release_allowed"] and rejected["reviewed_actions"] == [],
        "Rejected draft was released",
    )

    prior = runner.run(
        "history-seed",
        "analyze",
        "--input",
        str(ROOT / "samples/client_prior.json"),
        "--db",
        database,
        "--crm-fixture",
        str(CRM),
    )
    historical = runner.run("history-comparison", "analyze", *common)
    require(
        historical["sources"]["history_run_ids"] == [prior["run_id"]],
        "Historical selection mixed dates or sessions",
    )
    require(
        any(f["code"] == "NET_WORTH_DROP" for f in historical["findings"]),
        "Historical decline was missed",
    )

    for mode in ["none", "unavailable", "malformed"]:
        with runner.server(mode) as url:
            name = f"http-{mode}"
            report = runner.run(name, "analyze", *common, "--crm-url", url)
            require(report["status"] == "pending_review", f"{name}: release gate was bypassed")
            if mode == "none":
                require(report["execution_status"] == "completed", "HTTP CRM did not enrich data")
            else:
                require(report["execution_status"] == "degraded", f"{name}: outage was hidden")
                require(
                    not report["guardrails"]["portfolio_discussion_allowed"],
                    f"{name}: missing CRM did not block suitability",
                )
                events = [
                    json.loads(line)
                    for line in (runner.output / f"{name}.jsonl").read_text().splitlines()
                ]
                attempts = [
                    e
                    for e in events
                    if e["event"] == "tool.started" and e.get("tool") == "crm.profile"
                ]
                require(
                    len(attempts) == (3 if mode == "unavailable" else 1),
                    f"{name}: incorrect retry policy",
                )

    missing = runner.run(
        "missing-input",
        "analyze",
        "--input",
        str(runner.output / "missing"),
        "--db",
        database,
        expected_exit=1,
    )
    require(
        missing["actions"] == [] and not missing["advisory_release_allowed"],
        "Invalid input produced advisory actions",
    )
    original = Path(database).read_bytes()
    runner.run("protected-output", "analyze", *common, "--output", database, expected_exit=2)
    require(Path(database).read_bytes() == original, "Output collision changed the database")

    checks = runner.run("detector-catalog", "checks")
    require(len(checks["checks"]) == 18, "Detector catalog is incomplete")
    tools = runner.run("tool-capabilities", "tools")
    require("crm.profile" not in tools["agent_permissions"]["advisor"], "Scope leaked")
    configured = runner.run(
        "configured", "--config", str(ROOT / "config/settings.json"), "analyze", *common
    )
    require(
        configured["configuration"]["tools"]["circuit_threshold"] == 4, "Configuration was ignored"
    )
    bad_config = runner.output / "invalid-settings.json"
    bad_config.write_text('{"analysis":{"typo":1}}', encoding="utf-8")
    runner.run("invalid-config", "--config", str(bad_config), "analyze", *common, expected_exit=2)
    batch_common = [
        "--input-dir",
        str(ROOT / "samples/clients"),
        "--crm-fixture",
        str(ROOT / "samples/crm_batch.json"),
        "--db",
        database,
    ]
    batch = runner.run("batch-strict", "batch", *batch_common, expected_exit=1)
    require(batch["total"] == 3 and batch["failed"] == 1, "Batch did not isolate invalid import")
    batch = runner.run(
        "batch-lenient",
        "batch",
        *batch_common,
        "--ingestion-mode",
        "lenient",
        "--output-dir",
        str(runner.output / "batch-reports"),
    )
    require(batch["failed"] == 0 and len(batch["reports"]) == 3, "Lenient batch failed")
    repaired = next(r for r in batch["reports"] if r["client_id"] == "import_demo")
    require(repaired["data_quality"]["records_dropped"] == 2, "Repairs lack provenance")
    require(not repaired["guardrails"]["portfolio_discussion_allowed"], "Partial import cleared")

    decision = runner.output / "decision.json"
    pending = historical
    decision.write_text(
        json.dumps(
            {
                "decision": "approve",
                "reviewer": "Synthetic reviewer",
                "notes": "Demonstrate reusable feedback",
                "dismissals": {"F-LOW_LIQUIDITY": "Planned cash requirement"},
            }
        ),
        encoding="utf-8",
    )
    annotated = runner.run(
        "file-review", "review", pending["run_id"], "--db", database, "--decisions", str(decision)
    )
    require(annotated["findings"] == pending["findings"], "Annotation changed original evidence")
    require(
        "F-LOW_LIQUIDITY" not in {f["id"] for f in annotated["reviewed_findings"]},
        "Dismissal was not applied to reviewed findings",
    )
    require(
        "draft awaiting" not in annotated["reviewed_narrative"]["text"],
        "Reviewed narrative has draft state",
    )
    later_input = runner.output / "later-client.json"
    later_raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    later_raw["as_of"] = "2026-10-02"
    later_input.write_text(json.dumps(later_raw), encoding="utf-8")
    later = runner.run(
        "feedback-reuse",
        "analyze",
        "--input",
        str(later_input),
        "--db",
        database,
        "--crm-fixture",
        str(CRM),
    )
    require(
        "F-LOW_LIQUIDITY" not in {f["id"] for f in later["findings"]},
        "Identical evidence was not suppressed",
    )
    require(
        "F-LOW_LIQUIDITY" in {f["id"] for f in later["raw_findings"]},
        "Suppression removed raw evidence",
    )
    require(not later["advisory_release_allowed"], "Feedback bypassed new review")
    fallback = runner.run("narrative-fallback", "analyze", *common, "--llm")
    require(fallback["narrative"]["source"] == "template", "Unavailable model has no fallback")
    require(fallback["execution_status"] == "degraded", "Narrative fallback was hidden")
    cache = runner.run("cached-outage", "analyze", *common, "--simulate-crm-failure")
    require(cache["sources"]["crm_source"] == "cache", "Valid CRM cache was not used")
    require(not cache["guardrails"]["portfolio_discussion_allowed"], "Cache cleared suitability")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "output/validation")
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    output = args.output_root.resolve() / stamp
    output.mkdir(parents=True)
    runner = Runner(output)
    summary = {
        "created_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.system(),
        "passed": False,
        "scenarios": runner.results,
    }
    try:
        validate(runner)
        summary["passed"] = True
    except (ValidationFailed, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        summary["error"] = str(exc)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": summary["passed"],
                "scenario_count": len(runner.results),
                "artifacts": str(output),
                **({"error": summary["error"]} if "error" in summary else {}),
            },
            indent=2,
        )
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
