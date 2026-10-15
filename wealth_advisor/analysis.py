"""Detector catalog, independent failure boundaries, and compatible analysis entry points."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from . import detectors as checks
from . import detectors_extra as extra
from .domain import ClientData, CRMProfile, Finding, ValidationError
from .metrics import AnalysisContext
from .policy import AnalysisPolicy
from .priority import RANK


@dataclass(frozen=True)
class Detector:
    name: str
    function: Callable[[AnalysisContext], list[Finding]]
    needs: frozenset[str] = frozenset()
    description: str = ""


@dataclass
class AnalysisResult:
    metrics: dict[str, Any]
    findings: list[Finding]
    raw_findings: list[Finding]
    executed_checks: list[str]
    skipped_checks: list[dict[str, str]]
    failed_checks: list[dict[str, str]]


class DetectorRegistry:
    def __init__(self, detectors: list[Detector] | None = None):
        self.detectors = list(DEFAULT_DETECTORS if detectors is None else detectors)
        names = [d.name for d in self.detectors]
        if len(set(names)) != len(names):
            raise ValidationError("Detector names must be unique")

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": d.name, "needs": sorted(d.needs), "description": d.description or d.name}
            for d in self.detectors
        ]

    def run(self, context: AnalysisContext, selected: list[str] | None = None) -> AnalysisResult:
        known = {d.name for d in self.detectors}
        if selected is not None and not set(selected) <= known:
            raise ValidationError("Configuration references an unknown detector")
        raw, executed, skipped, failed = [], [], [], []
        for detector in self.detectors:
            if selected is not None and detector.name not in selected:
                skipped.append({"check": detector.name, "reason": "disabled_by_configuration"})
                continue
            missing = detector.needs - context.available
            if missing:
                skipped.append(
                    {
                        "check": detector.name,
                        "reason": "missing_inputs: " + ", ".join(sorted(missing)),
                    }
                )
                continue
            try:
                findings = detector.function(context)
                if not isinstance(findings, list) or any(
                    not isinstance(f, Finding) or f.severity not in RANK for f in findings
                ):
                    raise TypeError("Detector output violates the finding contract")
                raw.extend(findings)
                executed.append(detector.name)
            except Exception as exc:
                failed.append(
                    {
                        "check": detector.name,
                        "error_type": type(exc).__name__,
                        "reason": "detector_failed; findings from this check are unavailable",
                    }
                )
        return AnalysisResult(
            context.metrics, consolidate(raw), sort_findings(raw), executed, skipped, failed
        )


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (RANK[f.severity], f.id))


def consolidate(findings: list[Finding]) -> list[Finding]:
    grouped, other = defaultdict(list), []
    for finding in findings:
        tx_id = finding.evidence.get("transaction_id")
        if tx_id:
            grouped[tx_id].append(finding)
        else:
            other.append(finding)
    preference = {"LARGE_OUTFLOW": 0, "UNUSUAL_TRANSACTION": 1}
    for group in grouped.values():
        group.sort(key=lambda f: (RANK[f.severity], preference.get(f.code, 2), f.id))
        primary = group[0]
        if len(group) > 1:
            primary = replace(
                primary,
                evidence={
                    **primary.evidence,
                    "corroborating_signals": [
                        {"code": f.code, "evidence": f.evidence} for f in group[1:]
                    ],
                },
            )
        other.append(primary)
    return sort_findings(other)


def analyze_detailed(
    data: ClientData,
    profile: CRMProfile | None,
    history: list[dict[str, Any]],
    policy: AnalysisPolicy | None = None,
    registry: DetectorRegistry | None = None,
    selected: list[str] | None = None,
) -> AnalysisResult:
    return (registry or DetectorRegistry()).run(
        AnalysisContext(data, profile, history, policy or AnalysisPolicy()), selected
    )


def analyze(
    data: ClientData,
    profile: CRMProfile | None,
    history: list[dict[str, Any]],
    policy: AnalysisPolicy | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    """Compatibility API; orchestration uses analyze_detailed to expose coverage and failures."""
    result = analyze_detailed(data, profile, history, policy)
    return result.metrics, result.findings


DEFAULT_DETECTORS = [
    Detector("liquidity", checks.liquidity),
    Detector("cash_flow", checks.cash_flow),
    Detector("balance_sheet", checks.balance_sheet),
    Detector("concentration", checks.concentration),
    Detector("crm_context", checks.crm_context),
    Detector("suitability", checks.suitability, frozenset({"crm"})),
    Detector("baseline_coverage", checks.baseline_coverage),
    Detector("large_outflows", checks.large_outflows),
    Detector(
        "statistical_outflows", checks.statistical_outflows, frozenset({"statistical_baseline"})
    ),
    Detector("spending_growth", checks.spending_growth, frozenset({"complete_months"})),
    Detector("historical_comparison", checks.historical_comparison, frozenset({"history"})),
    Detector("new_counterparty", extra.new_counterparty, frozenset({"historical_outflows"})),
    Detector("income_drop", extra.income_drop, frozenset({"complete_months"})),
    Detector("duplicate_charges", extra.duplicate_charges, frozenset({"timestamps"})),
    Detector("transaction_velocity", extra.transaction_velocity, frozenset({"timestamps"})),
    Detector("odd_hours", extra.odd_hours, frozenset({"timestamps"})),
    Detector(
        "foreign_activity",
        extra.foreign_activity,
        frozenset({"home_country", "historical_outflows"}),
    ),
    Detector("structuring_pattern", extra.structuring_pattern, frozenset({"channels"})),
]
