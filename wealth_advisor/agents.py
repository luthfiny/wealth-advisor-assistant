"""Specialized agents depend only on the tool port and shared domain contracts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from .config import Settings
from .domain import Action, Session
from .feedback import apply_feedback, fingerprint
from .guards import evaluate_portfolio_guard
from .observability import emit
from .priority import RANK
from .tools import ToolClient, ToolError


class Agent(Protocol):
    name: str
    requires: frozenset[str]
    provides: frozenset[str]
    allowed_tools: frozenset[str]

    def run(self, session: Session, tools: ToolClient) -> None: ...


class DataFetcherAgent:
    name = "data_fetcher"
    requires = frozenset()
    provides = frozenset({"prepared_data"})
    allowed_tools = frozenset(
        {
            "financial.prepare",
            "crm.profile",
            "memory.history",
            "memory.feedback",
            "memory.crm.read",
            "memory.crm.save",
        }
    )

    def __init__(self, settings=None):
        self.settings = settings or Settings()

    def run(self, session: Session, tools: ToolClient) -> None:
        prepared = tools.call(
            "financial.prepare", {"path": session.input_path}, run_id=session.run_id
        )
        session.data, session.data_quality = prepared.data, prepared.quality
        if session.data_quality["issues"]:
            session.warnings.append(
                "Input records were repaired or dropped; inspect data_quality before review."
            )
        data = session.require_data()
        try:
            session.profile = tools.call(
                "crm.profile",
                {
                    "client_id": data.client_id,
                    "as_of": data.as_of,
                },
                run_id=session.run_id,
            )
            session.profile_source = "fresh"
            try:
                tools.call(
                    "memory.crm.save",
                    {"profile": session.profile, "as_of": data.as_of},
                    run_id=session.run_id,
                )
            except ToolError:
                session.warnings.append("CRM snapshot could not be cached.")
        except ToolError as exc:
            session.warnings.append(
                f"CRM context unavailable ({exc.code}); suitability is blocked."
            )
            emit(
                "agent.fallback",
                run_id=session.run_id,
                agent=self.name,
                decision="continue_without_crm",
                code=exc.code,
            )
            if exc.kind != "not_found":
                try:
                    cached = tools.call(
                        "memory.crm.read",
                        {
                            "client_id": data.client_id,
                            "as_of": data.as_of,
                            "max_age_days": self.settings.crm.cache_max_age_days,
                            "profile_max_age_days": self.settings.analysis.profile_max_age_days,
                        },
                        run_id=session.run_id,
                    )
                    if cached:
                        session.profile = cached["profile"]
                        session.profile_source = "cache"
                        session.warnings.append(
                            "A temporally valid cached CRM snapshot was used; "
                            "portfolio discussion remains blocked."
                        )
                        emit(
                            "agent.fallback",
                            run_id=session.run_id,
                            agent=self.name,
                            decision="use_cached_crm",
                            cache_age_days=cached["age_days"],
                        )
                except ToolError:
                    session.warnings.append(
                        "CRM cache unavailable; continuing without suitability context."
                    )
        try:
            session.history = tools.call(
                "memory.history",
                {
                    "client_id": data.client_id,
                    "currency": data.currency,
                    "as_of": data.as_of,
                },
                run_id=session.run_id,
            )
        except ToolError as exc:
            session.warnings.append(
                f"Historical memory unavailable ({exc.code}); using current data."
            )
            emit(
                "agent.fallback",
                run_id=session.run_id,
                agent=self.name,
                decision="continue_without_history",
                code=exc.code,
            )
        try:
            session.feedback = tools.call(
                "memory.feedback",
                {"client_id": data.client_id, "currency": data.currency, "as_of": data.as_of},
                run_id=session.run_id,
            )
        except ToolError:
            session.warnings.append(
                "Reviewer feedback unavailable; all current findings remain active."
            )


class AnalyzerAgent:
    name = "analyzer"
    requires = frozenset({"prepared_data"})
    provides = frozenset({"analysis"})
    allowed_tools = frozenset({"analytics.scan"})

    def __init__(self, settings=None):
        self.settings = settings or Settings()

    def run(self, session: Session, tools: ToolClient) -> None:
        result = tools.call(
            "analytics.scan",
            {
                "data": session.require_data(),
                "profile": session.profile,
                "history": session.history,
                "selected": self.settings.enabled_checks,
            },
            run_id=session.run_id,
        )
        session.metrics = result.metrics
        session.raw_findings, session.detector_signals = result.findings, result.raw_findings
        session.executed_checks, session.failed_checks = (
            result.executed_checks,
            result.failed_checks,
        )
        session.skipped_checks = result.skipped_checks
        session.findings, session.suppressed_findings, session.feedback_events = apply_feedback(
            result.findings,
            session.feedback,
            fingerprint(asdict(self.settings.analysis)),
            session.require_data().as_of,
        )
        session.findings.sort(key=lambda f: (RANK[f.severity], f.id))
        seen = {fid for h in session.history for fid in h.get("finding_ids", [])}
        session.recurring_ids = sorted(f.id for f in session.raw_findings if f.id in seen)
        if result.failed_checks:
            session.warnings.append(
                "Some detectors failed; inspect failed_checks and validate coverage manually."
            )
        emit(
            "agent.decision",
            run_id=session.run_id,
            agent=self.name,
            finding_count=len(session.findings),
            high_severity_count=sum(f.severity == "high" for f in session.findings),
        )


_ACTIONS = {
    "NEW_COUNTERPARTY": (
        "Confirm the new counterparty and the purpose of the payment with the client."
    ),
    "DUPLICATE_CHARGE": (
        "Reconcile the equal charges against receipts before requesting a correction."
    ),
    "TRANSACTION_VELOCITY": "Confirm whether the burst of outflows reflects planned activity.",
    "ODD_HOURS": "Verify the recorded timezone and confirm the overnight payment with the client.",
    "NEW_COUNTRY": "Confirm the new-country activity and verify the transaction records.",
    "STRUCTURING_PATTERN": (
        "Review the related cash movements and supporting records with the client."
    ),
    "INCOME_DROP": (
        "Confirm income receipt timing and review whether the monthly budget needs updating."
    ),
    "LOW_LIQUIDITY": (
        "Confirm near-term cash needs and discuss a staged reserve plan with the client."
    ),
    "NEGATIVE_CASH_FLOW": "Reconcile income and recurring expenses; agree on a sustainable budget.",
    "NEGATIVE_NET_WORTH": "Verify the balance sheet and review debt obligations with the client.",
    "EXPENSE_BASELINE_MISSING": (
        "Collect a reliable monthly expense estimate and rerun the analysis."
    ),
    "SINGLE_ASSET_CONCENTRATION": (
        "Verify the concentrated holding and discuss diversification after "
        "suitability, taxes and liquidity constraints are confirmed."
    ),
    "RISK_PROFILE_MISMATCH": "Validate risk tolerance and horizon with the client, then review "
    "whether the recorded equity exposure remains suitable.",
    "CRM_UNAVAILABLE": (
        "Restore CRM access and obtain current suitability and verification details."
    ),
    "STALE_PROFILE": "Refresh the client's risk profile and investment horizon in CRM.",
    "KYC_REVIEW": "Complete the outstanding client verification checks in CRM.",
    "LARGE_OUTFLOW": "Confirm the transaction's purpose and authorization with the client before "
    "drawing conclusions about unusual activity.",
    "UNUSUAL_TRANSACTION": "Reconcile the transaction against supporting records and ask the "
    "client whether it reflects a planned expense.",
    "SPENDING_GROWTH": "Review the category increase with the client and distinguish recurring "
    "costs from one-off purchases.",
    "NET_WORTH_DROP": "Reconcile the current and previous snapshots, including coverage changes, "
    "before discussing the apparent decline with the client.",
    "LIMITED_ANOMALY_BASELINE": "Collect more categorized transaction history to improve coverage.",
}


class AdvisorAgent:
    """An inspectable rule-based reasoning adapter, replaceable by a constrained LLM agent."""

    name = "advisor"
    requires = frozenset({"analysis"})
    provides = frozenset({"draft_actions"})
    allowed_tools = frozenset({"narrative.template", "narrative.llm"})

    def __init__(self, settings=None):
        self.settings = settings or Settings()

    def run(self, session: Session, tools: ToolClient) -> None:
        session.portfolio_guard = evaluate_portfolio_guard(session)
        suitability_blocked = not session.portfolio_guard.allowed
        for finding in session.findings:
            instruction = _ACTIONS.get(
                finding.code,
                "Review the supporting evidence manually before making a recommendation.",
            )
            if suitability_blocked and finding.code in {
                "RISK_PROFILE_MISMATCH",
                "SINGLE_ASSET_CONCENTRATION",
            }:
                instruction = (
                    "Verify recorded holdings and refresh CRM suitability information "
                    "before discussing any portfolio change."
                )
            session.actions.append(
                Action("A-" + finding.id[2:], (finding.id,), finding.severity, instruction)
            )
        brief = {
            "metrics": session.metrics,
            "findings": [asdict(f) for f in session.findings],
            "actions": [asdict(a) for a in session.actions],
            "warnings": session.warnings,
            "status": "pending_review",
        }
        if self.settings.narrative.enabled:
            try:
                session.narrative = tools.call(
                    "narrative.llm", {"brief": brief}, run_id=session.run_id
                )
            except ToolError as exc:
                session.warnings.append(
                    f"Optional narrative unavailable ({exc.code}); verified template used."
                )
                emit(
                    "agent.fallback",
                    run_id=session.run_id,
                    agent=self.name,
                    decision="use_template_narrative",
                    code=exc.code,
                )
        if not session.narrative:
            session.narrative = tools.call(
                "narrative.template", {"brief": brief}, run_id=session.run_id
            )
        emit(
            "agent.decision",
            run_id=session.run_id,
            agent=self.name,
            action_count=len(session.actions),
            suitability_blocked=suitability_blocked,
            portfolio_blockers=session.portfolio_guard.reasons,
            decision="request_human_review",
        )
