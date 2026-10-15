"""CLI commands; the composition root is runtime.py. JSON stdout, event logs stderr."""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .analysis import DetectorRegistry
from .config import Settings
from .domain import ValidationError, dumps
from .memory import MemoryError, SQLiteMemory
from .mock_crm import create_server
from .observability import configure_logging, emit
from .output import write_result as write_result
from .output import write_result_file
from .runtime import build_system as build_system
from .tools import ToolError, read_json


def _common(p):
    p.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    p.add_argument("--crm-fixture", type=Path, default=Path("samples/crm.json"))
    source = p.add_mutually_exclusive_group()
    source.add_argument("--crm-url")
    source.add_argument("--simulate-crm-failure", action="store_true")
    p.add_argument("--crm-timeout", type=float)
    p.add_argument("--llm", action="store_true", help="Optional Claude sentence selection")
    p.add_argument("--ingestion-mode", choices=["strict", "lenient"])
    p.add_argument("--output", type=Path)


def _parser():
    p = argparse.ArgumentParser(description="Auditable wealth advisor agent simulation")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    p.add_argument("--config", type=Path, help="Validated JSON settings")
    commands = p.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyze one client and save a draft for review")
    analyze.add_argument("--input", type=Path, required=True)
    _common(analyze)
    batch = commands.add_parser("batch", help="Analyze clients with independent failure boundaries")
    source = batch.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, nargs="+")
    source.add_argument("--input-dir", type=Path)
    batch.add_argument("--output-dir", type=Path)
    _common(batch)
    review = commands.add_parser("review", help="Approve or reject a persisted draft once")
    review.add_argument("run_id")
    review.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    mode = review.add_mutually_exclusive_group()
    mode.add_argument("--decision", choices=["approve", "reject"])
    mode.add_argument("--decisions", type=Path, help="JSON review decision")
    mode.add_argument("--interactive", action="store_true")
    review.add_argument("--reviewer")
    review.add_argument("--notes")
    review.add_argument("--override", action="append", default=[], metavar="ACTION_ID=TEXT")
    review.add_argument("--dismiss", action="append", default=[], metavar="FINDING_ID=REASON")
    review.add_argument("--severity", action="append", default=[], metavar="FINDING_ID=SEVERITY")
    review.add_argument("--feedback-ttl-days", type=int)
    review.add_argument("--output", type=Path)
    inspect = commands.add_parser("inspect", help="Read the original draft and final review")
    inspect.add_argument("run_id")
    inspect.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    inspect.add_argument("--output", type=Path)
    server = commands.add_parser("serve-crm", help="Run a loopback mock HTTP CRM")
    server.add_argument("--fixture", type=Path, default=Path("samples/crm.json"))
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--failure-mode", choices=["none", "unavailable", "malformed"], default="none"
    )
    commands.add_parser("tools", help="List tool capabilities and agent allowlists")
    commands.add_parser("checks", help="List detector requirements")
    return p


def _validate_paths(args):
    output = getattr(args, "output", None)
    if output is None:
        return
    sources = [getattr(args, name, None) for name in ("db", "crm_fixture", "config", "decisions")]
    input_value = getattr(args, "input", None)
    sources.extend(input_value if isinstance(input_value, list) else [input_value])
    if getattr(args, "input_dir", None):
        sources.extend(args.input_dir.glob("*.json"))
    for source in sources:
        if source is not None and (
            output.resolve() == source.resolve()
            or (output.exists() and source.exists() and output.samefile(source))
        ):
            raise ValidationError(
                "Output must not replace inputs, settings, decisions or the memory database"
            )


def _pairs(items, name):
    result = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key or key in result:
            raise ValidationError(f"Each {name} must use a unique ID=VALUE")
        result[key] = value
    return result


def _ask(prompt):
    print(prompt, end="", file=sys.stderr, flush=True)
    return input().strip()


def _review_arguments(args, settings):
    supplied = {
        "overrides": _pairs(args.override, "override"),
        "dismissals": _pairs(args.dismiss, "dismissal"),
        "severity_overrides": _pairs(args.severity, "severity override"),
    }
    ttl = (
        args.feedback_ttl_days
        if args.feedback_ttl_days is not None
        else settings.review.feedback_ttl_days
    )
    if args.decisions:
        if (
            any(supplied.values())
            or args.reviewer
            or args.notes
            or args.feedback_ttl_days is not None
        ):
            raise ValidationError("A decisions file cannot be combined with inline review fields")
        raw = read_json(args.decisions)
        if not isinstance(raw, dict) or set(raw) - {
            "decision",
            "reviewer",
            "notes",
            "overrides",
            "dismissals",
            "severity_overrides",
            "ttl_days",
        }:
            raise ValidationError("Review decision file has an invalid shape")
        for name in supplied:
            value = raw.get(name, {})
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValidationError("Review annotations must map string IDs to string values")
        return {
            "decision": raw.get("decision"),
            "reviewer": raw.get("reviewer"),
            "notes": raw.get("notes"),
            **{name: raw.get(name, {}) for name in supplied},
            "ttl_days": raw.get("ttl_days", ttl),
        }
    if args.interactive:
        report = SQLiteMemory(args.db).get(args.run_id)
        for finding in report["findings"]:
            print(f"{finding['id']}: [{finding['severity']}] {finding['title']}", file=sys.stderr)
        try:
            decision = _ask("Decision (approve/reject, blank cancels): ")
            if not decision:
                raise ValidationError("Review cancelled; the report remains pending")
            reviewer = args.reviewer or _ask("Reviewer: ")
            notes = args.notes or _ask("Validation notes: ")
        except EOFError as exc:
            raise ValidationError("Interactive input ended; no review was recorded") from exc
    else:
        decision, reviewer, notes = args.decision, args.reviewer, args.notes
    return {"decision": decision, "reviewer": reviewer, "notes": notes, **supplied, "ttl_days": ttl}


def _batch(args, settings):
    paths = args.input or sorted(args.input_dir.glob("*.json"))
    if not paths:
        raise ValidationError("Batch requires at least one JSON input file")
    if args.output_dir and (
        args.output_dir.resolve() == args.db.resolve()
        or any(args.output_dir.resolve() in {p.resolve(), p.parent.resolve()} for p in paths)
    ):
        raise ValidationError(
            "Batch output directory must be separate from inputs and the database"
        )
    system, _ = build_system(
        db_path=args.db,
        crm_fixture=args.crm_fixture,
        crm_url=args.crm_url,
        crm_timeout=args.crm_timeout,
        simulate_crm_failure=args.simulate_crm_failure,
        settings=settings,
    )
    reports = []
    for path in paths:
        report = system.run(path)
        if args.output_dir:
            try:
                write_result_file(report, args.output_dir / (report["run_id"] + ".json"))
            except OSError:
                report["export_error"] = "Individual report export failed"
        reports.append(report)
    failures = sum(r["status"] == "failed" or "export_error" in r for r in reports)
    result = {
        "schema_version": "1.1",
        "status": "partial_failed" if failures else "completed",
        "total": len(reports),
        "failed": failures,
        "reports": reports,
    }
    write_result(result, args.output)
    return 1 if failures else 0


def main(argv=None):
    args = _parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        _validate_paths(args)
        settings = Settings.load(args.config)
        if getattr(args, "llm", False):
            settings = replace(settings, narrative=replace(settings.narrative, enabled=True))
        if getattr(args, "ingestion_mode", None):
            settings = replace(settings, ingestion_mode=args.ingestion_mode)
        settings.validate()
        if args.command == "analyze":
            system, _ = build_system(
                db_path=args.db,
                crm_fixture=args.crm_fixture,
                crm_url=args.crm_url,
                crm_timeout=args.crm_timeout,
                simulate_crm_failure=args.simulate_crm_failure,
                settings=settings,
            )
            report = system.run(args.input)
            write_result(report, args.output)
            return 1 if report["status"] == "failed" else 0
        if args.command == "batch":
            return _batch(args, settings)
        if args.command == "review":
            decision = _review_arguments(args, settings)
            report = SQLiteMemory(args.db).review(args.run_id, **decision)
            emit(
                "review.recorded",
                run_id=args.run_id,
                decision=decision["decision"],
                override_count=len(decision["overrides"]),
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
        elif args.command == "checks":
            print(dumps({"checks": DetectorRegistry().catalog()}))
        elif args.command == "tools":
            system, _ = build_system(db_path=Path("data/memory.sqlite3"), settings=settings)
            print(
                dumps(
                    {
                        "tools": system.tools.describe(),
                        "agent_permissions": {
                            a.name: sorted(a.allowed_tools) for a in system.agents
                        },
                    }
                )
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValidationError, MemoryError, ToolError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, ToolError) else "command_error"
        message = "File or database operation failed" if isinstance(exc, OSError) else str(exc)
        print(dumps({"status": "failed", "error": {"code": code, "message": message}}))
        emit("command.failed", code=code, error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
