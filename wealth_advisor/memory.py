"""Durable summaries, original reports and append-only review decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .domain import ValidationError, dumps, text_value, utc_now


class MemoryError(RuntimeError):
    """Storage could not fulfill its contract."""


class Memory(Protocol):
    def history(self, client_id: str, currency: str, before: str) -> list[dict[str, Any]]: ...

    def save(self, report: dict[str, Any]) -> None: ...


class SQLiteMemory:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_history
                    ON runs(client_id, currency, as_of, created_at);
                CREATE TABLE IF NOT EXISTS reviews (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject')),
                    reviewer TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    overrides_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise MemoryError("Cannot open memory database") from exc
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def history(self, client_id: str, currency: str, before: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT r.run_id, r.as_of, r.report_json
                    FROM runs r LEFT JOIN reviews v ON v.run_id = r.run_id
                    WHERE r.client_id = ? AND r.currency = ? AND r.as_of < ?
                        AND (v.decision IS NULL OR v.decision != 'reject')
                    ORDER BY r.as_of DESC, r.created_at DESC LIMIT 5
                """,
                    (client_id, currency, before),
                ).fetchall()
            results = []
            for row in rows:
                report = json.loads(row["report_json"])
                # Invalid stored values should degrade history, not break current-data analysis.
                net_worth = Decimal(str(report["metrics"]["net_worth"]))
                runway = report["metrics"]["cash_runway_months"]
                if not net_worth.is_finite() or (
                    runway is not None and not Decimal(str(runway)).is_finite()
                ):
                    raise ValueError("Invalid historical metric")
                results.append(
                    {
                        "run_id": row["run_id"],
                        "as_of": row["as_of"],
                        "net_worth": report["metrics"]["net_worth"],
                        "cash_runway_months": report["metrics"]["cash_runway_months"],
                        "finding_codes": [f["code"] for f in report["findings"]],
                    }
                )
            return results
        except (sqlite3.Error, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise MemoryError("Cannot read historical summaries") from exc

    def save(self, report: dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        report["run_id"],
                        report["client_id"],
                        report["currency"],
                        report["as_of"],
                        report["created_at"],
                        dumps(report),
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            raise MemoryError("Cannot persist the report") from exc

    @staticmethod
    def _reviewed(report: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
        report["review"] = review
        if review is not None:
            approved = review["decision"] == "approve"
            report["status"] = "approved" if approved else "rejected"
            report["advisory_release_allowed"] = approved
            # Preserve the original actions; expose reviewed actions as a separate result.
            report["reviewed_actions"] = (
                [
                    {
                        **action,
                        "instruction": review["overrides"].get(action["id"], action["instruction"]),
                    }
                    for action in report["actions"]
                ]
                if approved
                else []
            )
        return report

    @staticmethod
    def _review_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "decision": row["decision"],
            "reviewer": row["reviewer"],
            "notes": row["notes"],
            "overrides": json.loads(row["overrides_json"]),
            "created_at": row["created_at"],
        }

    def get(self, run_id: str) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT report_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                review = connection.execute(
                    "SELECT * FROM reviews WHERE run_id = ?", (run_id,)
                ).fetchone()
            if row is None:
                raise ValidationError("Run does not exist in this memory database")
            return self._reviewed(json.loads(row["report_json"]), self._review_dict(review))
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise MemoryError("Cannot load the saved report") from exc

    def review(
        self,
        run_id: str,
        decision: str,
        reviewer: str,
        notes: str,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValidationError("Review decision must be approve or reject")
        reviewer = text_value(reviewer, "reviewer", limit=120)
        notes = text_value(notes, "notes", limit=2000)
        overrides = overrides or {}
        if decision == "reject" and overrides:
            raise ValidationError("Overrides can only accompany approval")
        for instruction in overrides.values():
            text_value(instruction, "override instruction", limit=1000)
        try:
            with self._connect() as connection:
                # Serialize read + decision so concurrent reviewers cannot both succeed.
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT report_json FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ValidationError("Run does not exist in this memory database")
                report = json.loads(row["report_json"])
                if report["status"] != "pending_review":
                    raise ValidationError("Only pending reports can be reviewed")
                action_ids = {a["id"] for a in report["actions"]}
                if not set(overrides).issubset(action_ids):
                    raise ValidationError("Override references an unknown action ID")
                if connection.execute(
                    "SELECT 1 FROM reviews WHERE run_id = ?", (run_id,)
                ).fetchone():
                    raise ValidationError("This run already has a final review decision")
                review = {
                    "decision": decision,
                    "reviewer": reviewer,
                    "notes": notes,
                    "overrides": overrides,
                    "created_at": utc_now(),
                }
                connection.execute(
                    "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        decision,
                        reviewer,
                        notes,
                        dumps(overrides),
                        review["created_at"],
                    ),
                )
            return self._reviewed(report, review)
        except ValidationError:
            raise
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise MemoryError("Cannot persist the review decision") from exc
