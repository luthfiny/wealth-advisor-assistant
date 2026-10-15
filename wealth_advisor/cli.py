"""Command-line composition root. JSON results go to stdout, event logs to stderr."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

from .domain import ValidationError, dumps
from .memory import MemoryError, SQLiteMemory
from .mock_crm import create_server
from .observability import configure_logging, emit
from .orchestrator import OrchestratorAgent
from .tools import (
    AnalyticsTool,
    FinancialDataTool,
    FixtureCRMTool,
    HistoryTool,
    HttpCRMTool,
    ToolError,
    ToolRegistry,
    read_json,
)


def build_system(
    *,
    db_path: Path,
    crm_fixture: Path = Path("samples/crm.json"),
    crm_url: str | None = None,
    simulate_crm_failure: bool = False,
    crm_timeout: float = 2.0,
) -> tuple[OrchestratorAgent, SQLiteMemory]:
    memory = SQLiteMemory(db_path)
    crm = (
        HttpCRMTool(crm_url, timeout=crm_timeout)
        if crm_url
        else FixtureCRMTool(crm_fixture, simulate_failure=simulate_crm_failure)
    )
    registry = ToolRegistry(
        {
            "financial.load": FinancialDataTool(),
            "crm.profile": crm,
            "analytics.scan": AnalyticsTool(),
            "memory.history": HistoryTool(memory),
        }
    )
    return OrchestratorAgent(registry, memory), memory


def write_result(report: dict[str, Any], output: Path | None) -> None:
    result = dumps(report) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=output.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(result)
            os.replace(temporary, output)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    print(result, end="")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auditable wealth advisor agent simulation")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Run agents and save a draft for human review")
    analyze.add_argument("--input", type=Path, required=True)
    analyze.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    analyze.add_argument("--output", type=Path)
    source = analyze.add_mutually_exclusive_group()
    source.add_argument("--crm-url", help="HTTP(S) mock CRM base URL")
    source.add_argument("--simulate-crm-failure", action="store_true")
    analyze.add_argument("--crm-fixture", type=Path, default=Path("samples/crm.json"))
    analyze.add_argument("--crm-timeout", type=float, default=2.0)

    review = commands.add_parser("review", help="Approve/reject a saved run exactly once")
    review.add_argument("run_id")
    review.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    review.add_argument("--decision", choices=["approve", "reject"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", required=True)
    review.add_argument("--override", action="append", default=[], metavar="ACTION_ID=INSTRUCTION")
    review.add_argument("--output", type=Path)

    inspect = commands.add_parser("inspect", help="Retrieve a saved run and its final review")
    inspect.add_argument("run_id")
    inspect.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    inspect.add_argument("--output", type=Path)

    server = commands.add_parser("serve-crm", help="Run the optional loopback mock HTTP API")
    server.add_argument("--fixture", type=Path, default=Path("samples/crm.json"))
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--failure-mode", choices=["none", "unavailable", "malformed"], default="none"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "analyze":
            system, _ = build_system(
                db_path=args.db,
                crm_fixture=args.crm_fixture,
                crm_url=args.crm_url,
                simulate_crm_failure=args.simulate_crm_failure,
                crm_timeout=args.crm_timeout,
            )
            report = system.run(args.input)
            write_result(report, args.output)
            return 1 if report["status"] == "failed" else 0
        if args.command == "review":
            overrides: dict[str, str] = {}
            for item in args.override:
                action_id, separator, instruction = item.partition("=")
                if not separator or action_id in overrides:
                    raise ValidationError("Each override must use a unique ACTION_ID=INSTRUCTION")
                overrides[action_id] = instruction
            report = SQLiteMemory(args.db).review(
                args.run_id, args.decision, args.reviewer, args.notes, overrides
            )
            emit(
                "review.recorded",
                run_id=args.run_id,
                decision=args.decision,
                override_count=len(overrides),
            )
            write_result(report, args.output)
        elif args.command == "inspect":
            write_result(SQLiteMemory(args.db).get(args.run_id), args.output)
        elif args.command == "serve-crm":
            profiles = read_json(args.fixture)
            if not isinstance(profiles, dict):
                raise ValidationError("CRM fixture must be an object")
            with create_server(profiles, port=args.port, failure_mode=args.failure_mode) as server:
                emit(
                    "mock_crm.started",
                    host="127.0.0.1",
                    port=server.server_port,
                    failure_mode=args.failure_mode,
                )
                server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValidationError, MemoryError, ToolError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, ToolError) else "command_error"
        # OSError messages can contain local paths; publish a stable storage error instead.
        message = "File or database operation failed" if isinstance(exc, OSError) else str(exc)
        print(dumps({"status": "failed", "error": {"code": code, "message": message}}))
        emit("command.failed", code=code, error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
