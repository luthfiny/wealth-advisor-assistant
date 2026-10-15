"""Deterministic analysis policy. All amounts retain Decimal precision."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from .domain import ClientData, CRMProfile, Finding

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class AnalysisPolicy:
    """Illustrative thresholds for this simulation, not investment suitability rules."""

    version: str = "2026-10-demo-v1"
    minimum_baseline: int = 8
    absolute_outflow: Decimal = Decimal("10000")
    minimum_spike: Decimal = Decimal("500")
    spike_ratio: Decimal = Decimal("4")
    robust_z: Decimal = Decimal("6")
    spending_growth: Decimal = Decimal("0.50")
    minimum_spending_increase: Decimal = Decimal("500")
    emergency_months: Decimal = Decimal("6")
    critical_cash_months: Decimal = Decimal("3")
    single_asset_limit: Decimal = Decimal("0.40")
    profile_max_age_days: int = 180
    conservative_equity_limit: Decimal = Decimal("0.40")
    moderate_equity_limit: Decimal = Decimal("0.65")
    aggressive_equity_limit: Decimal = Decimal("0.85")
    short_horizon_years: int = 3
    short_horizon_equity_limit: Decimal = Decimal("0.40")
    recent_days: int = 30
    baseline_days: int = 90
    net_worth_decline: Decimal = Decimal("0.20")


def _month_before(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def analyze(
    data: ClientData,
    profile: CRMProfile | None,
    history: list[dict[str, Any]],
    policy: AnalysisPolicy | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    policy = policy or AnalysisPolicy()
    findings: list[Finding] = []

    def add(
        code: str,
        severity: str,
        title: str,
        explanation: str,
        evidence: dict[str, Any],
        key: str = "",
    ) -> None:
        suffix = "-" + hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""
        findings.append(Finding(f"F-{code}{suffix}", code, severity, title, explanation, evidence))

    invested = sum((h.market_value for h in data.holdings), ZERO)
    assets = invested + data.cash_balance
    equity = sum((h.market_value for h in data.holdings if h.asset_class == "equity"), ZERO)
    runway = data.cash_balance / data.monthly_expenses if data.monthly_expenses else None
    monthly_surplus = data.monthly_income - data.monthly_expenses
    metrics: dict[str, Any] = {
        "currency": data.currency,
        "total_assets": assets,
        "invested_assets": invested,
        "total_liabilities": data.liabilities,
        "net_worth": assets - data.liabilities,
        "declared_monthly_surplus": monthly_surplus,
        "cash_runway_months": _rounded(runway) if runway is not None else None,
        "equity_share_of_investments": _rounded(equity / invested) if invested else None,
        "transaction_count": len(data.transactions),
        "policy": asdict(policy),
    }

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
    if assets - data.liabilities < 0:
        add(
            "NEGATIVE_NET_WORTH",
            "high",
            "Liabilities exceed recorded assets",
            "Confirm that the balance sheet includes all assets and liabilities.",
            {"assets": assets, "liabilities": data.liabilities},
        )

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

    recent_start = data.as_of - timedelta(days=policy.recent_days)
    baseline_start = recent_start - timedelta(days=policy.baseline_days)
    outflows = [t for t in data.transactions if t.kind in {"expense", "withdrawal"}]
    baseline = [t for t in outflows if baseline_start <= t.date < recent_start]
    recent = [t for t in outflows if recent_start <= t.date <= data.as_of]
    by_category: dict[str, list[Decimal]] = defaultdict(list)
    for tx in baseline:
        by_category[tx.category].append(tx.amount)
    eligible_categories = sorted(
        k for k, v in by_category.items() if len(v) >= policy.minimum_baseline
    )
    metrics["anomaly_coverage"] = {
        "baseline_start": max(baseline_start, data.history_start),
        "baseline_end": recent_start - timedelta(days=1),
        "recent_start": recent_start,
        "recent_end": data.as_of,
        "baseline_outflows": len(baseline),
        "recent_outflows": len(recent),
        "eligible_categories": eligible_categories,
    }
    if any(len(by_category[t.category]) < policy.minimum_baseline for t in recent) or not recent:
        add(
            "LIMITED_ANOMALY_BASELINE",
            "info",
            "Statistical coverage is limited",
            f"Some recent categories have fewer than {policy.minimum_baseline} historical "
            "outflows, or no recent "
            "outflows exist. Absolute amount rules still apply; absence of a flag is inconclusive.",
            {
                "eligible_categories": eligible_categories,
                "required_baseline_per_category": policy.minimum_baseline,
                "recent_outflows": len(recent),
            },
        )
    category_statistics: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
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
        reference = by_category[tx.category]
        if len(reference) < policy.minimum_baseline:
            continue
        if tx.category not in category_statistics:
            center = median(reference)
            mad = median([abs(amount - center) for amount in reference])
            # Each category has one historical baseline; reuse it for all recent outflows.
            # A ratio guard handles MAD=0 and avoids tiny-spend false alarms.
            threshold = max(
                policy.minimum_spike,
                center * policy.spike_ratio,
                center + policy.robust_z * Decimal("1.4826") * mad,
            )
            category_statistics[tx.category] = center, mad, threshold
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

    latest_month = _month_before(data.as_of)
    earlier_month = _month_before(latest_month)
    latest_end = data.as_of.replace(day=1) - timedelta(days=1)
    metrics["spending_comparison"] = {
        "available": data.history_start <= earlier_month,
        "previous_month": earlier_month,
        "latest_month": latest_month,
        "latest_end": latest_end,
    }
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
                and increase / previous > policy.spending_growth
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

    if history:
        prior = history[0]
        old_net_worth = Decimal(str(prior["net_worth"]))
        old_runway = prior.get("cash_runway_months")
        metrics["historical_comparison"] = {
            "previous_run_id": prior["run_id"],
            "previous_as_of": prior["as_of"],
            "net_worth_change": metrics["net_worth"] - old_net_worth,
            "cash_runway_change": (
                _rounded(runway - Decimal(str(old_runway)))
                if runway is not None and old_runway is not None
                else None
            ),
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
    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "info": 2}[f.severity], f.id))
    return metrics, findings
