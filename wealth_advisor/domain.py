"""Validated domain objects and JSON-safe, exact-decimal serialization."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class ValidationError(ValueError):
    """An input failed a documented contract."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(value, default=json_default, indent=indent, allow_nan=False)


def object_value(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def text_value(value: Any, name: str, *, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"{name} must be a nonempty string of at most {limit} characters")
    return value.strip()


def identifier(value: Any, name: str) -> str:
    result = text_value(value, name, limit=100)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", result):
        raise ValidationError(f"{name} must contain only letters, digits, underscores or hyphens")
    return result


def decimal_value(value: Any, name: str, *, money: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError(f"{name} must be a nonnegative decimal")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"{name} must be a nonnegative decimal") from exc
    if not result.is_finite() or result < 0 or result > Decimal("1000000000000000"):
        raise ValidationError(f"{name} must be finite, nonnegative and at most 10^15")
    if money and result != result.quantize(Decimal("0.01")):
        raise ValidationError(f"{name} must have at most two decimal places")
    return result


def date_value(value: Any, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError(f"{name} must use YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a valid calendar date") from exc
    if result.year < 1900:
        raise ValidationError(f"{name} must be on or after 1900-01-01")
    return result


def list_value(value: Any, name: str, *, maximum: int = 100_000) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError(f"{name} must be an array with at most {maximum} entries")
    return value


@dataclass(frozen=True)
class Transaction:
    id: str
    date: date
    amount: Decimal
    kind: str
    category: str
    counterparty: str


@dataclass(frozen=True)
class Holding:
    asset_id: str
    asset_class: str
    market_value: Decimal


@dataclass(frozen=True)
class ClientData:
    client_id: str
    as_of: date
    history_start: date
    currency: str
    cash_balance: Decimal
    monthly_income: Decimal
    monthly_expenses: Decimal
    liabilities: Decimal
    holdings: tuple[Holding, ...]
    transactions: tuple[Transaction, ...]

    @classmethod
    def parse(cls, raw: Any) -> ClientData:
        raw = object_value(raw, "client")
        client_id = identifier(raw.get("client_id"), "client_id")
        as_of = date_value(raw.get("as_of"), "as_of")
        history_start = date_value(raw.get("history_start"), "history_start")
        if history_start > as_of:
            raise ValidationError("history_start must be on or before as_of")
        currency = raw.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValidationError("currency must be a three-letter uppercase code")
        holdings: list[Holding] = []
        asset_ids: set[str] = set()
        for item in list_value(raw.get("holdings"), "holdings", maximum=10_000):
            item = object_value(item, "holding")
            asset_id = identifier(item.get("asset_id"), "asset_id")
            if asset_id in asset_ids:
                raise ValidationError("holdings contain duplicate asset_id values")
            asset_ids.add(asset_id)
            asset_class = item.get("asset_class")
            if not isinstance(asset_class, str) or asset_class not in {
                "equity",
                "bonds",
                "property",
                "alternative",
            }:
                raise ValidationError("asset_class must be equity, bonds, property or alternative")
            if item.get("currency", currency) != currency:
                raise ValidationError("mixed currencies require an FX adapter; normalize first")
            holdings.append(
                Holding(
                    asset_id, asset_class, decimal_value(item.get("market_value"), "market_value")
                )
            )
        transactions: list[Transaction] = []
        transaction_ids: set[str] = set()
        for item in list_value(raw.get("transactions"), "transactions"):
            item = object_value(item, "transaction")
            tx_id = identifier(item.get("id"), "transaction.id")
            if tx_id in transaction_ids:
                raise ValidationError("transactions contain duplicate id values")
            transaction_ids.add(tx_id)
            tx_date = date_value(item.get("date"), "transaction.date")
            if not history_start <= tx_date <= as_of:
                raise ValidationError("transaction dates must be within history_start and as_of")
            kind = item.get("kind")
            if not isinstance(kind, str) or kind not in {
                "expense",
                "withdrawal",
                "income",
                "refund",
                "internal_transfer",
            }:
                raise ValidationError("unsupported transaction kind")
            if item.get("currency", currency) != currency:
                raise ValidationError("mixed currencies require an FX adapter; normalize first")
            transactions.append(
                Transaction(
                    tx_id,
                    tx_date,
                    decimal_value(item.get("amount"), "transaction.amount"),
                    kind,
                    text_value(item.get("category"), "transaction.category", limit=80),
                    text_value(item.get("counterparty"), "transaction.counterparty", limit=120),
                )
            )
        return cls(
            client_id,
            as_of,
            history_start,
            currency,
            decimal_value(raw.get("cash_balance"), "cash_balance"),
            decimal_value(raw.get("monthly_income"), "monthly_income"),
            decimal_value(raw.get("monthly_expenses"), "monthly_expenses"),
            decimal_value(raw.get("liabilities"), "liabilities"),
            tuple(holdings),
            tuple(sorted(transactions, key=lambda tx: (tx.date, tx.id))),
        )


@dataclass(frozen=True)
class CRMProfile:
    client_id: str
    risk_tolerance: str
    investment_horizon_years: int
    kyc_status: str
    updated_at: date

    @classmethod
    def parse(cls, raw: Any, client_id: str, as_of: date) -> CRMProfile:
        raw = object_value(raw, "CRM profile")
        if raw.get("client_id") != client_id:
            raise ValidationError("CRM client_id does not match the requested client")
        risk = raw.get("risk_tolerance")
        if not isinstance(risk, str) or risk not in {"conservative", "moderate", "aggressive"}:
            raise ValidationError("CRM risk_tolerance is invalid")
        horizon = raw.get("investment_horizon_years")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or not 0 <= horizon <= 100:
            raise ValidationError("CRM investment_horizon_years must be an integer from 0 to 100")
        kyc = raw.get("kyc_status")
        if not isinstance(kyc, str) or kyc not in {"verified", "pending", "expired"}:
            raise ValidationError("CRM kyc_status is invalid")
        updated = date_value(raw.get("updated_at"), "CRM updated_at")
        if updated > as_of:
            raise ValidationError("CRM snapshot is newer than the analysis as_of date")
        return cls(client_id, risk, horizon, kyc, updated)


@dataclass(frozen=True)
class Finding:
    id: str
    code: str
    severity: str
    title: str
    explanation: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Action:
    id: str
    finding_ids: tuple[str, ...]
    priority: str
    instruction: str
    requires_human_review: bool = True


@dataclass
class Session:
    """Short-term memory shared only within one orchestrated run."""

    run_id: str
    input_path: str
    data: ClientData | None = None
    profile: CRMProfile | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)

    def require_data(self) -> ClientData:
        if self.data is None:
            raise RuntimeError("data preparation must complete before analysis")
        return self.data
