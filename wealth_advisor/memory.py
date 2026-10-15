"""Durable summaries, original reports and append-only review decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .domain import CRMProfile, ValidationError, dumps, text_value, utc_now
from .feedback import evidence_fingerprint, fingerprint
from .priority import RANK, review_priority


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
                CREATE TABLE IF NOT EXISTS review_annotations (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    dismissals_json TEXT NOT NULL,
                    severity_json TEXT NOT NULL,
                    ttl_days INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS finding_feedback (
                    source_run_id TEXT NOT NULL REFERENCES runs(run_id),
                    finding_id TEXT NOT NULL, client_id TEXT NOT NULL, currency TEXT NOT NULL,
                    source_as_of TEXT NOT NULL, policy_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL, decision TEXT NOT NULL, severity TEXT,
                    reason TEXT NOT NULL, reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_on TEXT NOT NULL,
                    PRIMARY KEY (source_run_id, finding_id)
                );
                CREATE INDEX IF NOT EXISTS feedback_lookup
                    ON finding_feedback(client_id, currency, source_as_of);
                CREATE TABLE IF NOT EXISTS crm_snapshots (
                    client_id TEXT NOT NULL, observed_as_of TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    PRIMARY KEY (client_id, observed_as_of)
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
                        "finding_ids": [
                            f["id"] for f in report.get("raw_findings", report["findings"])
                        ],
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
            dismissed = review.get("dismissals", {})
            severities = review.get("severity_overrides", {})
            reviewed_findings = (
                [
                    {**f, "severity": severities.get(f["id"], f["severity"])}
                    for f in report["findings"]
                    if f["id"] not in dismissed
                ]
                if approved
                else []
            )
            by_id = {f["id"]: f for f in reviewed_findings}
            report["reviewed_findings"] = reviewed_findings
            report["reviewed_priority"] = review_priority(reviewed_findings) if approved else None
            report["reviewed_actions"] = (
                [
                    {
                        **action,
                        "instruction": review["overrides"].get(action["id"], action["instruction"]),
                        "finding_ids": [fid for fid in action["finding_ids"] if fid in by_id],
                        "priority": min(
                            (
                                by_id[fid]["severity"]
                                for fid in action["finding_ids"]
                                if fid in by_id
                            ),
                            key=RANK.get,
                        ),
                    }
                    for action in report["actions"]
                    if any(fid in by_id for fid in action["finding_ids"])
                ]
                if approved
                else []
            )
            reviewed_findings.sort(key=lambda f: (RANK[f["severity"]], f["id"]))
            report["reviewed_actions"].sort(key=lambda a: (RANK[a["priority"]], a["id"]))

            from .narrative import render_brief

            report["reviewed_narrative"] = (
                render_brief(
                    {
                        "metrics": report["metrics"],
                        "findings": reviewed_findings,
                        "actions": report["reviewed_actions"],
                        "warnings": report.get("warnings", []),
                        "status": report["status"],
                    }
                )
                if approved
                else None
            )
        return report

    @staticmethod
    def _review_dict(row: sqlite3.Row | None, annotations=None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "decision": row["decision"],
            "reviewer": row["reviewer"],
            "notes": row["notes"],
            "overrides": json.loads(row["overrides_json"]),
            "created_at": row["created_at"],
            "dismissals": json.loads(annotations["dismissals_json"]) if annotations else {},
            "severity_overrides": json.loads(annotations["severity_json"]) if annotations else {},
            "feedback_ttl_days": annotations["ttl_days"] if annotations else None,
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
                annotations = connection.execute(
                    "SELECT * FROM review_annotations WHERE run_id = ?", (run_id,)
                ).fetchone()
            if row is None:
                raise ValidationError("Run does not exist in this memory database")
            return self._reviewed(
                json.loads(row["report_json"]), self._review_dict(review, annotations)
            )
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
        dismissals: dict[str, str] | None = None,
        severity_overrides: dict[str, str] | None = None,
        ttl_days: int = 90,
    ) -> dict[str, Any]:
        if not isinstance(decision, str) or decision not in {"approve", "reject"}:
            raise ValidationError("Review decision must be approve or reject")
        reviewer = text_value(reviewer, "reviewer", limit=120)
        notes = text_value(notes, "notes", limit=2000)
        overrides = {} if overrides is None else overrides
        dismissals = {} if dismissals is None else dismissals
        severity_overrides = {} if severity_overrides is None else severity_overrides
        for mapping in (overrides, dismissals, severity_overrides):
            if not isinstance(mapping, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
            ):
                raise ValidationError("Review annotations must map string IDs to string values")
        if decision == "reject" and (overrides or dismissals or severity_overrides):
            raise ValidationError("Overrides can only accompany approval")
        if isinstance(ttl_days, bool) or not isinstance(ttl_days, int) or not 1 <= ttl_days <= 3650:
            raise ValidationError("Feedback expiry must be from 1 to 3650 days")
        if set(dismissals) & set(severity_overrides):
            raise ValidationError("A finding cannot be dismissed and severity-overridden together")
        for instruction in overrides.values():
            text_value(instruction, "override instruction", limit=1000)
        for reason in dismissals.values():
            text_value(reason, "dismissal reason", limit=2000)
        if any(severity not in RANK for severity in severity_overrides.values()):
            raise ValidationError("Unknown severity override")
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
                finding_ids = {f["id"] for f in report["findings"]}
                if not (set(dismissals) | set(severity_overrides)) <= finding_ids:
                    raise ValidationError("Review annotation references an unknown finding ID")
                if any(
                    a["id"] in overrides and set(a["finding_ids"]) <= set(dismissals)
                    for a in report["actions"]
                ):
                    raise ValidationError(
                        "Cannot override an action whose findings are all dismissed"
                    )
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
                    "dismissals": dismissals,
                    "severity_overrides": severity_overrides,
                    "feedback_ttl_days": ttl_days,
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
                connection.execute(
                    "INSERT INTO review_annotations VALUES (?, ?, ?, ?)",
                    (run_id, dumps(dismissals), dumps(severity_overrides), ttl_days),
                )
                original = {f["id"]: f for f in report.get("raw_findings", report["findings"])}
                policy_id = report.get("policy_id", fingerprint(report["metrics"]["policy"]))
                try:
                    expiry = (
                        (date.fromisoformat(report["as_of"]) + timedelta(days=ttl_days)).isoformat()
                        if dismissals or severity_overrides
                        else None
                    )
                except OverflowError as exc:
                    raise ValidationError("Feedback expiry exceeds the supported calendar") from exc
                for fid in set(dismissals) | set(severity_overrides):
                    connection.execute(
                        "INSERT INTO finding_feedback "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            fid,
                            report["client_id"],
                            report["currency"],
                            report["as_of"],
                            policy_id,
                            evidence_fingerprint(original[fid]),
                            "dismiss" if fid in dismissals else "severity",
                            severity_overrides.get(fid),
                            dismissals.get(fid, notes),
                            reviewer,
                            review["created_at"],
                            expiry,
                        ),
                    )
            return self._reviewed(report, review)
        except ValidationError:
            raise
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise MemoryError("Cannot persist the review decision") from exc

    def feedback(self, client_id: str, currency: str, as_of: date):
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT * FROM finding_feedback
                    WHERE client_id = ? AND currency = ? AND source_as_of < ?
                    ORDER BY source_as_of DESC, created_at DESC LIMIT 10000""",
                    (client_id, currency, as_of.isoformat()),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise MemoryError("Cannot read reviewer feedback") from exc

    def cache_crm(self, profile: CRMProfile, as_of: date):
        CRMProfile.parse(
            asdict(profile) | {"updated_at": profile.updated_at.isoformat()},
            profile.client_id,
            as_of,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO crm_snapshots VALUES (?, ?, ?)",
                    (profile.client_id, as_of.isoformat(), dumps(profile)),
                )
        except sqlite3.Error as exc:
            raise MemoryError("Cannot cache CRM profile") from exc

    def cached_crm(self, client_id: str, as_of: date, max_age_days: int, profile_max_age_days: int):
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT * FROM crm_snapshots WHERE client_id = ?
                    AND observed_as_of <= ? ORDER BY observed_as_of DESC LIMIT 1""",
                    (client_id, as_of.isoformat()),
                ).fetchone()
            if row is None:
                return None
            observed = date.fromisoformat(row["observed_as_of"])
            profile = CRMProfile.parse(json.loads(row["profile_json"]), client_id, as_of)
            if (as_of - observed).days > max_age_days or (
                as_of - profile.updated_at
            ).days > profile_max_age_days:
                return None
            return {
                "profile": profile,
                "observed_as_of": observed,
                "age_days": (as_of - observed).days,
            }
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise MemoryError("Cannot read a valid CRM cache snapshot") from exc
