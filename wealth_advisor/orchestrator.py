"""Central dependency-aware coordinator, execution trace and release checkpoint."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agents import AdvisorAgent, Agent, AnalyzerAgent, DataFetcherAgent
from .config import Settings
from .domain import Session, utc_now
from .feedback import fingerprint
from .memory import Memory, MemoryError
from .observability import emit
from .priority import review_priority
from .tool_runtime import ToolError, ToolProvider


class OrchestratorAgent:
    def __init__(
        self, tools: ToolProvider, memory: Memory, agents: list[Agent] | None = None, settings=None
    ):
        if not isinstance(tools, ToolProvider):
            raise TypeError("Orchestrator tools must provide scoped agent clients")
        self.tools = tools
        self.memory = memory
        self.settings = settings or Settings()
        self.agents = (
            agents
            if agents is not None
            else [
                DataFetcherAgent(self.settings),
                AnalyzerAgent(self.settings),
                AdvisorAgent(self.settings),
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
                allowed = getattr(agent, "allowed_tools", frozenset())
                if not isinstance(allowed, (set, frozenset)):
                    allowed = frozenset()
                scoped = self.tools.scoped(
                    allowed,
                    observer=lambda metadata, agent_name=agent.name: session.tool_calls.append(
                        {"agent": agent_name, **metadata}
                    ),
                )
                agent.run(session, scoped)
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
                "schema_version": "1.1",
                "run_id": session.run_id,
                "created_at": started,
                "status": "failed",
                "execution_status": "failed",
                "advisory_release_allowed": False,
                "error": {"code": code, "message": message},
                "events": session.events,
                "tool_calls": session.tool_calls,
                "warnings": session.warnings,
                "actions": [],
                "reviewed_actions": [],
                "memory_persisted": False,
            }

        report: dict[str, Any] = {
            "schema_version": "1.1",
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
            "raw_findings": [asdict(f) for f in session.raw_findings],
            "detector_signals": [asdict(f) for f in session.detector_signals],
            "suppressed_findings": session.suppressed_findings,
            "feedback_events": session.feedback_events,
            "recurring_finding_ids": session.recurring_ids,
            "review_priority": review_priority(session.findings),
            "original_review_priority": review_priority(session.raw_findings),
            "policy_id": fingerprint(asdict(self.settings.analysis)),
            "configuration": self.settings.to_dict(),
            "data_quality": session.data_quality,
            "executed_checks": session.executed_checks,
            "skipped_checks": session.skipped_checks,
            "failed_checks": session.failed_checks,
            "narrative": session.narrative,
            "tool_calls": session.tool_calls,
            "actions": [asdict(a) for a in session.actions],
            "reviewed_actions": [],
            "guardrails": {
                "human_review_required": True,
                "portfolio_discussion_allowed": session.portfolio_guard.allowed,
                "portfolio_blockers": list(session.portfolio_guard.reasons),
            },
            "sources": {
                "financial_data": "provided_json",
                "crm_available": session.profile is not None,
                "crm_source": session.profile_source,
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
