"""Shared metric preparation; detector selection never changes financial calculations."""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .domain import ClientData, CRMProfile
from .policy import AnalysisPolicy

ZERO = Decimal("0")


def rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def month_before(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


@dataclass
class AnalysisContext:
    data: ClientData
    profile: CRMProfile | None
    history: list[dict[str, Any]]
    policy: AnalysisPolicy
    metrics: dict[str, Any] = field(init=False)

    def __post_init__(self):
        d, p = self.data, self.policy
        self.invested = sum((h.market_value for h in d.holdings), ZERO)
        self.assets = self.invested + d.cash_balance
        self.equity = sum((h.market_value for h in d.holdings if h.asset_class == "equity"), ZERO)
        self.runway = d.cash_balance / d.monthly_expenses if d.monthly_expenses else None
        self.monthly_surplus = d.monthly_income - d.monthly_expenses
        self.recent_start = d.as_of - timedelta(days=p.recent_days)
        baseline_start = self.recent_start - timedelta(days=p.baseline_days)
        self.outflows = [t for t in d.transactions if t.kind in {"expense", "withdrawal"}]
        self.baseline = [t for t in self.outflows if baseline_start <= t.date < self.recent_start]
        self.recent = [t for t in self.outflows if self.recent_start <= t.date <= d.as_of]
        self.by_category: dict[str, list[Decimal]] = defaultdict(list)
        for tx in self.baseline:
            self.by_category[tx.category].append(tx.amount)
        self.eligible_categories = sorted(
            k for k, v in self.by_category.items() if len(v) >= p.minimum_baseline
        )
        self.latest_month = month_before(d.as_of)
        self.earlier_month = month_before(self.latest_month)
        self.latest_end = d.as_of.replace(day=1) - timedelta(days=1)
        self.metrics = {
            "currency": d.currency,
            "total_assets": self.assets,
            "invested_assets": self.invested,
            "total_liabilities": d.liabilities,
            "net_worth": self.assets - d.liabilities,
            "declared_monthly_surplus": self.monthly_surplus,
            "cash_runway_months": rounded(self.runway) if self.runway is not None else None,
            "equity_share_of_investments": rounded(self.equity / self.invested)
            if self.invested
            else None,
            "transaction_count": len(d.transactions),
            "policy": asdict(p),
            "anomaly_coverage": {
                "baseline_start": max(baseline_start, d.history_start),
                "baseline_end": self.recent_start - timedelta(days=1),
                "recent_start": self.recent_start,
                "recent_end": d.as_of,
                "baseline_outflows": len(self.baseline),
                "recent_outflows": len(self.recent),
                "eligible_categories": self.eligible_categories,
            },
            "spending_comparison": {
                "available": d.history_start <= self.earlier_month,
                "previous_month": self.earlier_month,
                "latest_month": self.latest_month,
                "latest_end": self.latest_end,
            },
        }

    @property
    def available(self) -> set[str]:
        flags = set()
        if self.profile is not None:
            flags.add("crm")
        if self.history:
            flags.add("history")
        if self.data.history_start <= self.earlier_month:
            flags.add("complete_months")
        if self.eligible_categories:
            flags.add("statistical_baseline")
        if len(self.baseline) >= self.policy.minimum_baseline:
            flags.add("historical_outflows")
        if self.recent and all(t.timestamp is not None for t in self.recent):
            flags.add("timestamps")
        if self.data.home_country:
            flags.add("home_country")
        if self.recent and all(t.channel for t in self.recent):
            flags.add("channels")
        return flags
