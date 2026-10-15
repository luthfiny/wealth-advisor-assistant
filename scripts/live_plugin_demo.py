"""Call the packaged MCP plugin over stdio and save the live request/result transcript."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    if args.plugin_dir:
        manifest = json.loads((args.plugin_dir / ".mcp.json").read_text(encoding="utf-8"))
        connection = manifest["mcpServers"]["wealth_advisor"]
        params = StdioServerParameters(
            command=connection["command"], args=connection["args"], cwd=connection.get("cwd")
        )
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "wealth_advisor.mcp_server",
                "--workspace",
                str(ROOT),
                "--data-dir",
                str(args.output / "memory"),
            ],
            cwd=ROOT,
        )
    args.output.mkdir(parents=True, exist_ok=False)
    transcript, checks = [], []

    async def call(client, name, arguments, *, expect_error=False):
        started = time.perf_counter()
        result = await client.call_tool(name, arguments)
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        entry = {
            "tool": name,
            "arguments": arguments,
            "duration_ms": elapsed,
            "is_error": result.is_error,
            "result": result.structured_content,
            "content": [c.model_dump(mode="json", by_alias=True) for c in result.content],
        }
        transcript.append(entry)
        (args.output / "transcript.json").write_text(
            json.dumps(transcript, indent=2) + "\n", encoding="utf-8"
        )
        if bool(result.is_error) != expect_error:
            raise RuntimeError(f"Unexpected MCP error state for {name}")
        return result.structured_content

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)
        checks.append(message)

    started = time.perf_counter()
    async with Client(
        params, read_timeout_seconds=30, mode="legacy" if args.legacy else "auto"
    ) as client:
        connection_ms = round((time.perf_counter() - started) * 1000, 2)
        tools = (await client.list_tools()).tools
        print(
            json.dumps(
                {
                    "event": "connected",
                    "server": client.server_info.name,
                    "protocol_version": client.protocol_version,
                    "connection_ms": connection_ms,
                    "tools": [t.name for t in tools],
                }
            ),
            flush=True,
        )
        await call(client, "list_demo_clients", {})
        capabilities = await call(client, "get_capabilities", {})
        require(len(capabilities["checks"]) == 18, "18 detectors advertised")
        reports = []
        for sample in args.sample:
            report = await call(
                client,
                "analyze_client",
                {
                    "sample": sample,
                    "ingestion_mode": "lenient" if sample == "import_repairs" else "strict",
                },
            )
            reports.append(report)
            require(not report["advisory_release_allowed"], f"{sample}: human gate intact")
            require(report["memory_persisted"], f"{sample}: persisted")
            require(
                isinstance(report["metrics"]["net_worth"], str), f"{sample}: exact money strings"
            )
            trace = await call(client, "get_run_trace", {"run_id": report["run_id"]})
            require(
                [e["agent"] for e in trace["events"]] == ["data_fetcher", "analyzer", "advisor"],
                f"{sample}: three-agent routing",
            )
            persisted = await call(client, "get_report", {"run_id": report["run_id"]})
            require(persisted == report, f"{sample}: report survives roundtrip")
            crm = next(c for c in trace["tool_calls"] if c["tool"] == "crm.profile")
            print(
                json.dumps(
                    {
                        "event": "analyzed",
                        "sample": sample,
                        "run_id": report["run_id"],
                        "execution_status": report["execution_status"],
                        "net_worth": report["metrics"]["net_worth"],
                        "finding_count": len(report["findings"]),
                        "review_priority": report["review_priority"],
                        "crm_source": report["sources"]["crm_source"],
                        "crm_attempts": crm["attempts"],
                        "agent_trace": trace["events"],
                        "narrative": report["narrative"]["text"],
                        "advisory_release_allowed": report["advisory_release_allowed"],
                    }
                ),
                flush=True,
            )
        if args.outage:
            report = await call(
                client,
                "analyze_client",
                {
                    "sample": args.sample[0],
                    "crm_mode": "outage",
                    "ingestion_mode": "lenient" if args.sample[0] == "import_repairs" else "strict",
                },
            )
            crm = next(c for c in report["tool_calls"] if c["tool"] == "crm.profile")
            require(crm["attempts"] == 3, "outage: three HTTP attempts")
            require(report["sources"]["crm_source"] == "cache", "outage: dated cache used")
            require(
                not report["guardrails"]["portfolio_discussion_allowed"],
                "outage: portfolio guard intact",
            )
            require(not report["advisory_release_allowed"], "outage: human gate intact")
            print(
                json.dumps(
                    {
                        "event": "outage_tested",
                        "run_id": report["run_id"],
                        "execution_status": report["execution_status"],
                        "crm_source": report["sources"]["crm_source"],
                        "crm_attempts": crm["attempts"],
                        "warnings": report["warnings"],
                        "portfolio_discussion_allowed": report["guardrails"][
                            "portfolio_discussion_allowed"
                        ],
                    }
                ),
                flush=True,
            )
        await call(client, "analyze_client", {"sample": "unknown"}, expect_error=True)
        await call(client, "analyze_client", {"sample": "import_repairs"}, expect_error=True)
        await call(client, "get_report", {"run_id": "not-a-run"}, expect_error=True)
        await call(client, "not_a_tool", {}, expect_error=True)
        require(
            not (await call(client, "get_report", {"run_id": reports[0]["run_id"]}))[
                "advisory_release_allowed"
            ],
            "invalid requests did not corrupt an existing draft",
        )
        summary = {
            "passed": True,
            "transport": "stdio",
            "server": client.server_info.name,
            "protocol_version": client.protocol_version,
            "connection_ms": connection_ms,
            "tool_call_count": len(transcript),
            "checks": checks,
            "artifacts": str(args.output),
            "run_ids": [r["run_id"] for r in reports],
        }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path)
    parser.add_argument(
        "--sample",
        nargs="+",
        choices=["original", "balanced", "activity", "import_repairs"],
        default=["original", "balanced", "activity", "import_repairs"],
    )
    parser.add_argument("--outage", action="store_true")
    parser.add_argument("--legacy", action="store_true", help="Exercise classic MCP initialization")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / ("live-plugin-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")),
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
