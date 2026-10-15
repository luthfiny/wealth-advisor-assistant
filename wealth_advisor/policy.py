"""Versioned, exact-decimal analysis thresholds."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AnalysisPolicy:
    """Illustrative thresholds for this simulation, not investment suitability rules."""

    version: str = "2026-10-demo-v2"
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
    new_counterparty_minimum: Decimal = Decimal("2000")
    income_drop_fraction: Decimal = Decimal("0.30")
    duplicate_window_minutes: int = 10
    velocity_count: int = 8
    velocity_window_minutes: int = 60
    odd_hours_minimum: Decimal = Decimal("1000")
    foreign_minimum: Decimal = Decimal("2000")
    structuring_minimum: Decimal = Decimal("8000")
    structuring_maximum: Decimal = Decimal("10000")
    structuring_count: int = 3
    structuring_window_days: int = 7
