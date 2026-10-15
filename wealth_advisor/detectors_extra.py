"""Additional transaction and income checks with explicit evidence and temporal windows."""

from collections import defaultdict
from datetime import timedelta

from .detectors import FindingSink
from .domain import Finding
from .metrics import ZERO, AnalysisContext, rounded


def _evidence(tx):
    return {
        "transaction_id": tx.id,
        "date": tx.date,
        "amount": tx.amount,
        "category": tx.category,
        "counterparty": tx.counterparty,
    }


def new_counterparty(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    seen = {t.counterparty.casefold() for t in c.baseline}
    for tx in c.recent:
        if (
            tx.amount >= c.policy.new_counterparty_minimum
            and tx.counterparty.casefold() not in seen
        ):
            sink.add(
                "NEW_COUNTERPARTY",
                "medium",
                "First high-value outflow to a counterparty",
                "The counterparty is absent from the earlier outflow baseline; confirm purpose.",
                {**_evidence(tx), "minimum_amount": c.policy.new_counterparty_minimum},
                tx.id,
            )
    return sink.items


def income_drop(c: AnalysisContext) -> list[Finding]:
    previous = sum(
        (
            t.amount
            for t in c.data.transactions
            if t.kind == "income" and c.earlier_month <= t.date < c.latest_month
        ),
        ZERO,
    )
    latest = sum(
        (
            t.amount
            for t in c.data.transactions
            if t.kind == "income" and c.latest_month <= t.date <= c.latest_end
        ),
        ZERO,
    )
    sink = FindingSink()
    if previous > 0 and (previous - latest) / previous > c.policy.income_drop_fraction:
        sink.add(
            "INCOME_DROP",
            "high",
            "Recorded income declined",
            "Income receipts declined between two complete covered months. "
            "Confirm coverage and timing.",
            {
                "previous_month": c.earlier_month,
                "latest_month": c.latest_month,
                "previous_income": previous,
                "latest_income": latest,
                "decline_fraction": rounded((previous - latest) / previous),
                "threshold": c.policy.income_drop_fraction,
            },
        )
    return sink.items


def duplicate_charges(c: AnalysisContext) -> list[Finding]:
    grouped = defaultdict(list)
    for tx in c.recent:
        if tx.kind == "expense" and tx.amount > 0:
            grouped[(tx.counterparty.casefold(), tx.amount)].append(tx)
    sink = FindingSink()
    for group in grouped.values():
        group.sort(key=lambda tx: tx.timestamp)
        clusters = []
        for tx in group:
            if clusters and tx.timestamp - clusters[-1][0].timestamp <= timedelta(
                minutes=c.policy.duplicate_window_minutes
            ):
                clusters[-1].append(tx)
            else:
                clusters.append([tx])
        for cluster in clusters:
            if len(cluster) >= 2:
                ids = sorted(t.id for t in cluster)
                sink.add(
                    "DUPLICATE_CHARGE",
                    "medium",
                    "Possible duplicate charges",
                    "Equal charges to the same counterparty occurred within the configured window.",
                    {
                        "transaction_ids": ids,
                        "amount_each": cluster[0].amount,
                        "counterparty": cluster[0].counterparty,
                        "window_minutes": c.policy.duplicate_window_minutes,
                    },
                    "|".join(ids),
                )
    return sink.items


def transaction_velocity(c: AnalysisContext) -> list[Finding]:
    ordered = sorted(c.recent, key=lambda tx: tx.timestamp)
    left, best_left, best_right = 0, 0, 0
    for right, tx in enumerate(ordered):
        while tx.timestamp - ordered[left].timestamp > timedelta(
            minutes=c.policy.velocity_window_minutes
        ):
            left += 1
        if right + 1 - left > best_right - best_left:
            best_left, best_right = left, right + 1
    sink = FindingSink()
    if best_right - best_left >= c.policy.velocity_count:
        window = ordered[best_left:best_right]
        sink.add(
            "TRANSACTION_VELOCITY",
            "high",
            "Burst of recent outflows",
            "Outflow count reached the configured threshold within a short time window.",
            {
                "transaction_ids": sorted(t.id for t in window),
                "count": len(window),
                "window_minutes": c.policy.velocity_window_minutes,
                "start": window[0].timestamp,
                "end": window[-1].timestamp,
            },
            window[0].id,
        )
    return sink.items


def odd_hours(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    for tx in c.recent:
        if 1 <= tx.timestamp.hour < 5 and tx.amount >= c.policy.odd_hours_minimum:
            sink.add(
                "ODD_HOURS",
                "medium",
                "Large outflow during overnight hours",
                "The supplied timestamp is between 01:00 and 05:00 in its recorded offset.",
                {**_evidence(tx), "timestamp": tx.timestamp},
                tx.id,
            )
    return sink.items


def foreign_activity(c: AnalysisContext) -> list[Finding]:
    sink = FindingSink()
    seen = {t.country for t in c.baseline if t.country}
    for tx in c.recent:
        if (
            tx.country
            and tx.country != c.data.home_country
            and tx.country not in seen
            and tx.amount >= c.policy.foreign_minimum
        ):
            sink.add(
                "NEW_COUNTRY",
                "medium",
                "High-value outflow in a new country",
                "The country differs from the client's home country and earlier observed activity.",
                {**_evidence(tx), "country": tx.country, "home_country": c.data.home_country},
                tx.id,
            )
    return sink.items


def structuring_pattern(c: AnalysisContext) -> list[Finding]:
    cash = sorted(
        (
            t
            for t in c.recent
            if t.channel == "cash"
            and c.policy.structuring_minimum <= t.amount < c.policy.structuring_maximum
        ),
        key=lambda tx: (tx.date, tx.id),
    )
    sink, left, right = FindingSink(), 0, 0
    while left < len(cash):
        right = max(right, left + 1)
        while (
            right < len(cash)
            and (cash[right].date - cash[left].date).days <= c.policy.structuring_window_days
        ):
            right += 1
        if right - left >= c.policy.structuring_count:
            group = cash[left:right]
            ids = sorted(t.id for t in group)
            sink.add(
                "STRUCTURING_PATTERN",
                "high",
                "Repeated cash movements near a review threshold",
                "A demo pattern for manual investigation; "
                "it does not establish evasion or wrongdoing.",
                {
                    "transaction_ids": ids,
                    "total_amount": sum((t.amount for t in group), ZERO),
                    "lower_bound": c.policy.structuring_minimum,
                    "upper_bound_exclusive": c.policy.structuring_maximum,
                    "window_days": c.policy.structuring_window_days,
                },
                "|".join(ids),
            )
            left = right
        else:
            left += 1
    return sink.items
