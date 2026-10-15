"""Central dependency-aware coordinator, execution trace and release checkpoint."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agents import AdvisorAgent, Agent, AnalyzerAgent, DataFetcherAgent
from .domain import Session, utc_now
from .memory import Memory, MemoryError
from .observability import emit
from .tools import ToolClient, ToolError


class OrchestratorAgent:
    def __init__(self, tools: ToolClient, memory: Memory, agents: list[Agent] | None = None):
        self.tools = tools
        self.memory = memory
        self.agents = (
            agents
            if agents is not None
            else [
                DataFetcherAgent(),
                AnalyzerAgent(),
                AdvisorAgent(),
            ]
        )
        names = [a.name for a in self.agents]
        if len(names) != len(set(names)):
            raise ValueError("Agent names must be unique")

    def run(self, input_path: Path) -> dict[str, Any]:
        session = Session(str(uuid4()), str(input_path))
        started = utc_now()
        emit("run.started", run_id=session.run_id)
        remaining = list(self.agents)
        try:
            while remaining:
                eligible = [a for a in remaining if a.requires <= session.completed]
                if not eligible:
                    raise ToolError("invalid_agent_plan", "Agent dependencies cannot be satisfied")
                agent = eligible[0]
                emit(
                    "orchestrator.route",
                    run_id=session.run_id,
                    agent=agent.name,
                    satisfied_dependencies=sorted(agent.requires),
                )
                step_start = time.monotonic()
                session.events.append({"agent": agent.name, "status": "started"})
                agent.run(session, self.tools)
                session.completed.update(agent.provides)
                session.events[-1].update(
                    {
                        "status": "completed",
                        "duration_ms": round((time.monotonic() - step_start) * 1000, 2),
                    }
                )
                remaining.remove(agent)
            if "draft_actions" not in session.completed:
                raise ToolError("invalid_agent_plan", "Agent plan did not produce draft actions")
            data = session.require_data()
        except Exception as exc:
            code = exc.code if isinstance(exc, ToolError) else "agent_internal_error"
            message = str(exc) if isinstance(exc, ToolError) else "An agent failed unexpectedly"
            if session.events and session.events[-1]["status"] == "started":
                session.events[-1]["status"] = "failed"
            emit(
                "run.failed",
                level=logging.ERROR,
                run_id=session.run_id,
                code=code,
                error_type=type(exc).__name__,
            )
            return {
                "schema_version": "1.0",
                "run_id": session.run_id,
                "created_at": started,
                "status": "failed",
                "execution_status": "failed",
                "advisory_release_allowed": False,
                "error": {"code": code, "message": message},
                "events": session.events,
                "warnings": session.warnings,
                "actions": [],
                "reviewed_actions": [],
                "memory_persisted": False,
            }

        report: dict[str, Any] = {
            "schema_version": "1.0",
            "run_id": session.run_id,
            "created_at": started,
            "client_id": data.client_id,
            "as_of": data.as_of.isoformat(),
            "currency": data.currency,
            "status": "pending_review",
            "execution_status": "degraded" if session.warnings else "completed",
            "advisory_release_allowed": False,
            "reasoning_mode": "deterministic_policy",
            "summary": {
                "finding_count": len(session.findings),
                "high_severity_count": sum(f.severity == "high" for f in session.findings),
                "action_count": len(session.actions),
            },
            "metrics": session.metrics,
            "findings": [asdict(f) for f in session.findings],
            "actions": [asdict(a) for a in session.actions],
            "reviewed_actions": [],
            "guardrails": {
                "human_review_required": True,
                "portfolio_discussion_allowed": not any(
                    f.code in {"CRM_UNAVAILABLE", "STALE_PROFILE", "KYC_REVIEW"}
                    for f in session.findings
                ),
            },
            "sources": {
                "financial_data": "provided_json",
                "crm_available": session.profile is not None,
                "crm_profile": asdict(session.profile) if session.profile else None,
                "history_run_ids": [h["run_id"] for h in session.history],
            },
            "warnings": session.warnings,
            "events": session.events,
            "review": None,
            "memory_persisted": True,
        }
        try:
            self.memory.save(report)
        except (MemoryError, OSError) as exc:
            report["memory_persisted"] = False
            report["execution_status"] = "degraded"
            report["warnings"].append(
                "Report memory could not be saved; review cannot proceed "
                "until storage is restored and analysis is rerun."
            )
            emit(
                "memory.save_failed",
                level=logging.WARNING,
                run_id=session.run_id,
                error_type=type(exc).__name__,
            )
        emit(
            "run.awaiting_review",
            run_id=session.run_id,
            status=report["execution_status"],
            memory_persisted=report["memory_persisted"],
            finding_count=len(session.findings),
        )
        return report
