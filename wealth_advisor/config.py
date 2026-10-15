"""Validated JSON settings. Unknown fields and invalid threshold relationships fail early."""

import math
from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import ValidationError, decimal_value, loads
from .policy import AnalysisPolicy
from .tool_runtime import ToolPolicy


@dataclass(frozen=True)
class ReviewPolicy:
    feedback_ttl_days: int = 90


@dataclass(frozen=True)
class CRMSettings:
    cache_max_age_days: int = 30
    timeout_seconds: float = 2.0


@dataclass(frozen=True)
class NarrativeSettings:
    enabled: bool = False
    model: str = "claude-haiku-4-5"
    timeout_seconds: float = 5.0
    max_tokens: int = 256


@dataclass(frozen=True)
class Settings:
    analysis: AnalysisPolicy = field(default_factory=AnalysisPolicy)
    tools: ToolPolicy = field(default_factory=lambda: ToolPolicy(circuit_threshold=4))
    review: ReviewPolicy = field(default_factory=ReviewPolicy)
    crm: CRMSettings = field(default_factory=CRMSettings)
    narrative: NarrativeSettings = field(default_factory=NarrativeSettings)
    enabled_checks: tuple[str, ...] | None = None
    ingestion_mode: str = "strict"
    max_in_flight: int = 4

    @classmethod
    def load(cls, path: Path | None = None):
        if path is None:
            return cls()
        try:
            raw = loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValidationError("Cannot read a valid settings JSON file") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any):
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValidationError("Settings must be an object with recognized fields")
        settings = cls()
        values = {}
        for key, value in raw.items():
            current = getattr(settings, key)
            if key in {"analysis", "tools", "review", "crm", "narrative"}:
                if not isinstance(value, dict) or set(value) - {f.name for f in fields(current)}:
                    raise ValidationError(f"Unknown or invalid settings in {key}")
                parsed = {}
                for name, item in value.items():
                    default = getattr(current, name)
                    if isinstance(default, bool):
                        if not isinstance(item, bool):
                            raise ValidationError(f"{key}.{name} must be boolean")
                    elif isinstance(default, Decimal):
                        item = decimal_value(item, f"{key}.{name}", money=False)
                        if item.as_tuple().exponent < -12:
                            raise ValidationError(
                                f"{key}.{name} supports at most 12 decimal places"
                            )
                    elif isinstance(default, int):
                        if (
                            isinstance(item, bool)
                            or not isinstance(item, int)
                            or not 0 <= item <= 100000
                        ):
                            raise ValidationError(
                                f"{key}.{name} must be a bounded nonnegative integer"
                            )
                    elif isinstance(default, float):
                        if isinstance(item, bool) or not isinstance(item, (int, float, Decimal)):
                            raise ValidationError(f"{key}.{name} must be numeric")
                        item = float(item)
                        if not math.isfinite(item) or not 0 <= item <= 3600:
                            raise ValidationError(f"{key}.{name} must be finite and at most 3600")
                    elif not isinstance(item, str) or not item.strip() or len(item) > 120:
                        raise ValidationError(f"{key}.{name} must be a nonempty short string")
                    parsed[name] = item
                values[key] = replace(current, **parsed)
            elif key == "enabled_checks":
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(n, str) for n in value)
                    or len(set(value)) != len(value)
                ):
                    raise ValidationError(
                        "enabled_checks must be null or a list of unique detector names"
                    )
                values[key] = tuple(value) if value is not None else None
            elif key == "ingestion_mode":
                if value not in ("strict", "lenient"):
                    raise ValidationError("ingestion_mode must be strict or lenient")
                values[key] = value
            elif key == "max_in_flight":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
                    raise ValidationError("max_in_flight must be an integer from 1 to 32")
                values[key] = value
        result = replace(settings, **values)
        result.validate()
        return result

    def validate(self):
        p = self.analysis
        fractions = (
            p.spending_growth,
            p.income_drop_fraction,
            p.single_asset_limit,
            p.conservative_equity_limit,
            p.moderate_equity_limit,
            p.aggressive_equity_limit,
            p.short_horizon_equity_limit,
            p.net_worth_decline,
        )
        if any(not 0 <= n <= 1 for n in fractions):
            raise ValidationError("Fraction thresholds must be between zero and one")
        if (
            p.minimum_baseline < 1
            or p.recent_days < 1
            or p.baseline_days < 1
            or p.duplicate_window_minutes < 1
            or p.velocity_count < 2
            or p.velocity_window_minutes < 1
            or p.structuring_count < 2
            or p.structuring_window_days < 1
            or p.structuring_minimum >= p.structuring_maximum
            or p.critical_cash_months > p.emergency_months
            or not p.conservative_equity_limit
            <= p.moderate_equity_limit
            <= p.aggressive_equity_limit
        ):
            raise ValidationError("Analysis threshold relationships are invalid")
        if (
            not 1 <= self.tools.max_attempts <= 10
            or self.tools.deadline_seconds <= 0
            or self.tools.circuit_cooldown_seconds <= 0
            or not 0 < self.crm.timeout_seconds <= 30
            or not 0 < self.narrative.timeout_seconds <= 30
            or not 1 <= self.review.feedback_ttl_days <= 3650
            or self.narrative.max_tokens < 32
        ):
            raise ValidationError("Execution and review bounds are invalid")

    def to_dict(self):
        return asdict(self)
