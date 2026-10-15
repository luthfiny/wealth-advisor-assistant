"""Independent detector functions, adapted from the original validated policy."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Any

from .domain import Finding
from .metrics import ZERO, AnalysisContext
from .metrics import rounded as _rounded


class FindingSink:
    def __init__(self):
        self.items = []

    def add(self, code, severity, title, explanation, evidence, key=""):
        suffix = "-" + hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""
        self.items.append(
            Finding(f"F-{code}{suffix}", code, severity, title, explanation, evidence)
        )


def liquidity(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    policy = c.policy
    runway = c.runway
    if runway is None:
        add(
            "EXPENSE_BASELINE_MISSING",
            "medium",
            "Cash coverage cannot be estimated",
            "Monthly expenses are zero. Confirm the expense baseline before assessing liquidity.",
            {"monthly_expenses": data.monthly_expenses},
        )
    elif runway < policy.emergency_months:
        add(
            "LOW_LIQUIDITY",
            "high" if runway < policy.critical_cash_months else "medium",
            "Limited cash reserve",
            "Cash is below the illustrative emergency reserve target.",
            {
                "cash_balance": data.cash_balance,
                "monthly_expenses": data.monthly_expenses,
                "coverage_months": _rounded(runway),
                "target_months": policy.emergency_months,
                "target_gap": max(
                    ZERO, data.monthly_expenses * policy.emergency_months - data.cash_balance
                ),
            },
        )
    return sink.items


def cash_flow(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    monthly_surplus = c.monthly_surplus
    if monthly_surplus < 0:
        add(
            "NEGATIVE_CASH_FLOW",
            "high",
            "Declared expenses exceed income",
            "The declared monthly budget has a deficit; transaction coverage may differ.",
            {
                "monthly_income": data.monthly_income,
                "monthly_expenses": data.monthly_expenses,
                "monthly_deficit": -monthly_surplus,
            },
        )
    return sink.items


def balance_sheet(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    assets = c.assets
    if assets - data.liabilities < 0:
        add(
            "NEGATIVE_NET_WORTH",
            "high",
            "Liabilities exceed recorded assets",
            "Confirm that the balance sheet includes all assets and liabilities.",
            {"assets": assets, "liabilities": data.liabilities},
        )
    return sink.items


def concentration(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    policy = c.policy
    invested = c.invested
    if invested:
        for holding in data.holdings:
            share = holding.market_value / invested
            if share > policy.single_asset_limit:
                add(
                    "SINGLE_ASSET_CONCENTRATION",
                    "high",
                    "Investment concentration",
                    "One recorded asset exceeds the illustrative concentration threshold.",
                    {
                        "asset_id": holding.asset_id,
                        "market_value": holding.market_value,
                        "share_of_investments": _rounded(share),
                        "limit": policy.single_asset_limit,
                    },
                    holding.asset_id,
                )
    return sink.items


def crm_context(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    profile = c.profile
    policy = c.policy
    if profile is None:
        add(
            "CRM_UNAVAILABLE",
            "high",
            "Client suitability context is unavailable",
            "Refresh the CRM profile before discussing portfolio changes.",
            {"source": "crm"},
        )
    else:
        age = (data.as_of - profile.updated_at).days
        if age > policy.profile_max_age_days:
            add(
                "STALE_PROFILE",
                "high",
                "CRM profile needs refresh",
                "The profile is older than the demo's freshness threshold.",
                {
                    "updated_at": profile.updated_at,
                    "age_days": age,
                    "limit_days": policy.profile_max_age_days,
                },
            )
        if profile.kyc_status != "verified":
            add(
                "KYC_REVIEW",
                "high",
                "Client verification is incomplete",
                "Resolve the CRM verification status before discussing portfolio changes.",
                {"kyc_status": profile.kyc_status},
            )
    return sink.items


def suitability(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    profile = c.profile
    policy = c.policy
    invested = c.invested
    equity = c.equity
    equity_limit = {
        "conservative": policy.conservative_equity_limit,
        "moderate": policy.moderate_equity_limit,
        "aggressive": policy.aggressive_equity_limit,
    }[profile.risk_tolerance]
    if profile.investment_horizon_years < policy.short_horizon_years:
        equity_limit = min(equity_limit, policy.short_horizon_equity_limit)
    if invested and equity / invested > equity_limit:
        add(
            "RISK_PROFILE_MISMATCH",
            "high",
            "Equity exposure merits suitability review",
            "Equity exposure exceeds the demo limit for the recorded risk profile and horizon.",
            {
                "risk_tolerance": profile.risk_tolerance,
                "investment_horizon_years": profile.investment_horizon_years,
                "equity_share": _rounded(equity / invested),
                "limit": equity_limit,
            },
        )
    return sink.items


def baseline_coverage(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    policy = c.policy
    recent = c.recent
    by_category = c.by_category
    eligible_categories = c.eligible_categories
    if any(len(by_category[t.category]) < policy.minimum_baseline for t in recent) or not recent:
        add(
            "LIMITED_ANOMALY_BASELINE",
            "info",
            "Statistical coverage is limited",
            f"Some recent categories have fewer than {policy.minimum_baseline} historical "
            "outflows, or no recent outflows exist. Absolute amount rules still apply; "
            "absence of a flag is inconclusive.",
            {
                "eligible_categories": eligible_categories,
                "required_baseline_per_category": policy.minimum_baseline,
                "recent_outflows": len(recent),
            },
        )
    return sink.items


def spending_growth(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    data = c.data
    policy = c.policy
    latest_month = c.latest_month
    earlier_month = c.earlier_month
    latest_end = c.latest_end
    if data.history_start <= earlier_month:
        monthly: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
        for tx in data.transactions:
            if tx.kind != "expense":
                continue
            if earlier_month <= tx.date < latest_month:
                monthly[tx.category][0] += tx.amount
            elif latest_month <= tx.date <= latest_end:
                monthly[tx.category][1] += tx.amount
        for category, (previous, latest) in sorted(monthly.items()):
            increase = latest - previous
            if (
                previous > 0
                and increase >= policy.minimum_spending_increase
                and (increase / previous > policy.spending_growth)
            ):
                add(
                    "SPENDING_GROWTH",
                    "medium",
                    "Category spending increased",
                    "Spending rose between two complete calendar months in the supplied history.",
                    {
                        "category": category,
                        "previous_month": earlier_month,
                        "latest_month": latest_month,
                        "previous_amount": previous,
                        "latest_amount": latest,
                        "increase": increase,
                        "growth_fraction": _rounded(increase / previous),
                    },
                    category,
                )
    return sink.items


def historical_comparison(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    history = c.history
    policy = c.policy
    metrics = c.metrics
    runway = c.runway
    if history:
        prior = history[0]
        old_net_worth = Decimal(str(prior["net_worth"]))
        old_runway = prior.get("cash_runway_months")
        metrics["historical_comparison"] = {
            "previous_run_id": prior["run_id"],
            "previous_as_of": prior["as_of"],
            "net_worth_change": metrics["net_worth"] - old_net_worth,
            "cash_runway_change": _rounded(runway - Decimal(str(old_runway)))
            if runway is not None and old_runway is not None
            else None,
        }
        if old_net_worth > 0 and metrics["net_worth"] < old_net_worth * (
            Decimal("1") - policy.net_worth_decline
        ):
            add(
                "NET_WORTH_DROP",
                "high",
                "Recorded net worth decline exceeds the review threshold",
                "Compare source coverage and cash movements before attributing a cause.",
                {
                    "previous_run_id": prior["run_id"],
                    "previous_as_of": prior["as_of"],
                    "previous_net_worth": old_net_worth,
                    "current_net_worth": metrics["net_worth"],
                    "decline_threshold": policy.net_worth_decline,
                },
            )
    return sink.items


def large_outflows(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    policy = c.policy
    recent = c.recent
    for tx in recent:
        evidence: dict[str, Any] = {
            "transaction_id": tx.id,
            "date": tx.date,
            "amount": tx.amount,
            "category": tx.category,
        }
        if tx.amount >= policy.absolute_outflow:
            add(
                "LARGE_OUTFLOW",
                "high",
                "Large recent outflow",
                "The outflow exceeds the demo's absolute review threshold; confirm its purpose.",
                {**evidence, "threshold": policy.absolute_outflow},
                tx.id,
            )
            continue
    return sink.items


def statistical_outflows(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    add = sink.add
    policy = c.policy
    recent = c.recent
    by_category = c.by_category
    category_statistics: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for tx in recent:
        evidence: dict[str, Any] = {
            "transaction_id": tx.id,
            "date": tx.date,
            "amount": tx.amount,
            "category": tx.category,
        }
        reference = by_category[tx.category]
        if len(reference) < policy.minimum_baseline:
            continue
        if tx.category not in category_statistics:
            center = median(reference)
            mad = median([abs(amount - center) for amount in reference])
            threshold = max(
                policy.minimum_spike,
                center * policy.spike_ratio,
                center + policy.robust_z * Decimal("1.4826") * mad,
            )
            category_statistics[tx.category] = (center, mad, threshold)
        center, mad, threshold = category_statistics[tx.category]
        if tx.amount > threshold:
            add(
                "UNUSUAL_TRANSACTION",
                "medium",
                "Unusual transaction amount",
                "The outflow exceeds both its category's robust historical band and ratio guard.",
                {
                    **evidence,
                    "baseline_count": len(reference),
                    "median": center,
                    "median_absolute_deviation": mad,
                    "threshold": _rounded(threshold),
                },
                tx.id,
            )
    return sink.items
