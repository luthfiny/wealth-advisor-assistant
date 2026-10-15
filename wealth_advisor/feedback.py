"""Evidence-bound review memory. Changes never mutate raw findings or financial measurements."""

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

from .domain import Finding, json_default


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, default=json_default, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def evidence_fingerprint(finding: Finding | dict) -> str:
    value = asdict(finding) if isinstance(finding, Finding) else finding
    return fingerprint(
        {key: value[key] for key in ("id", "code", "severity", "title", "explanation", "evidence")}
    )


def apply_feedback(findings, feedback, policy_id, as_of):
    active, suppressed, events = [], [], []
    latest = {}
    for entry in feedback:
        latest.setdefault(entry["finding_id"], entry)
    for finding in findings:
        entry = latest.get(finding.id)
        reason = None
        if entry:
            if entry["policy_id"] != policy_id:
                reason = "policy_changed"
            elif entry["evidence_hash"] != evidence_fingerprint(finding):
                reason = "evidence_changed"
            elif entry["expires_on"] < as_of.isoformat():
                reason = "expired"
            if reason:
                events.append({"finding_id": finding.id, "outcome": reason})
            elif entry["decision"] == "dismiss":
                suppressed.append({**asdict(finding), "feedback": entry})
                continue
            elif entry["decision"] == "severity":
                finding = replace(finding, severity=entry["severity"])
                events.append(
                    {"finding_id": finding.id, "outcome": "severity_applied", "feedback": entry}
                )
        active.append(finding)
    return active, suppressed, events
