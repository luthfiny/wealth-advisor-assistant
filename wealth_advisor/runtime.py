"""Composition root for concrete tools, scoped agents, settings and memory."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .analysis import DetectorRegistry
from .config import Settings
from .domain import ValidationError
from .memory import SQLiteMemory
from .narrative import ClaudeNarrativeTool, TemplateNarrativeTool
from .orchestrator import OrchestratorAgent
from .tools import (
    AnalyticsTool,
    FinancialDataTool,
    FixtureCRMTool,
    HistoryTool,
    HttpCRMTool,
    InputPreparationTool,
    MemoryTool,
    ToolRegistry,
)


def build_system(
    *,
    db_path: Path,
    crm_fixture: Path = Path("samples/crm.json"),
    crm_url: str | None = None,
    simulate_crm_failure: bool = False,
    crm_timeout: float | None = None,
    settings: Settings | None = None,
    detector_registry: DetectorRegistry | None = None,
) -> tuple[OrchestratorAgent, SQLiteMemory]:
    settings = settings or Settings()
    settings.validate()
    detectors = detector_registry or DetectorRegistry()
    if settings.enabled_checks is not None and not set(settings.enabled_checks) <= {
        d.name for d in detectors.detectors
    }:
        raise ValidationError("Settings reference an unknown detector")
    memory = SQLiteMemory(db_path)
    crm = (
        HttpCRMTool(
            crm_url,
            timeout=crm_timeout if crm_timeout is not None else settings.crm.timeout_seconds,
        )
        if crm_url
        else FixtureCRMTool(crm_fixture, simulate_failure=simulate_crm_failure)
    )
    registry = ToolRegistry(
        {
            "financial.load": FinancialDataTool(),
            "financial.prepare": InputPreparationTool(settings.ingestion_mode),
            "crm.profile": crm,
            "analytics.scan": AnalyticsTool(detectors, settings.analysis),
            "memory.history": HistoryTool(memory),
            "memory.feedback": MemoryTool(memory, "feedback"),
            "memory.crm.read": MemoryTool(memory, "cached_crm"),
            "memory.crm.save": MemoryTool(memory, "cache_crm"),
            "narrative.template": TemplateNarrativeTool(),
            "narrative.llm": ClaudeNarrativeTool(settings.narrative),
        },
        policies={
            "crm.profile": settings.tools,
            "narrative.llm": replace(
                settings.tools, max_attempts=1, deadline_seconds=settings.narrative.timeout_seconds
            ),
        },
        max_in_flight=settings.max_in_flight,
        default_policy=settings.tools,
    )
    return OrchestratorAgent(registry, memory, settings=settings), memory
