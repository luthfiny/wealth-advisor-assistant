"""Specialized agents depend only on the tool port and shared domain contracts."""

from __future__ import annotations

from typing import Protocol

from .domain import Action, Session
from .observability import emit
from .tools import ToolClient, ToolError


class Agent(Protocol):
    name: str
    requires: frozenset[str]
    provides: frozenset[str]

    def run(self, session: Session, tools: ToolClient) -> None: ...


class DataFetcherAgent:
    name = "data_fetcher"
    requires = frozenset()
    provides = frozenset({"prepared_data"})

    def run(self, session: Session, tools: ToolClient) -> None:
        session.data = tools.call(
            "financial.load", {"path": session.input_path}, run_id=session.run_id
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


class AnalyzerAgent:
    name = "analyzer"
    requires = frozenset({"prepared_data"})
    provides = frozenset({"analysis"})

    def run(self, session: Session, tools: ToolClient) -> None:
        session.metrics, session.findings = tools.call(
            "analytics.scan",
            {
                "data": session.require_data(),
                "profile": session.profile,
                "history": session.history,
            },
            run_id=session.run_id,
        )
        emit(
            "agent.decision",
            run_id=session.run_id,
            agent=self.name,
            finding_count=len(session.findings),
            high_severity_count=sum(f.severity == "high" for f in session.findings),
        )


_ACTIONS = {
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

    def run(self, session: Session, tools: ToolClient) -> None:
        blockers = {"CRM_UNAVAILABLE", "STALE_PROFILE", "KYC_REVIEW"}
        suitability_blocked = any(f.code in blockers for f in session.findings)
        for finding in session.findings:
            instruction = _ACTIONS[finding.code]
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
        emit(
            "agent.decision",
            run_id=session.run_id,
            agent=self.name,
            action_count=len(session.actions),
            suitability_blocked=suitability_blocked,
            decision="request_human_review",
        )
