"""A transparent triage heuristic, never a probability or investment risk estimate."""

from decimal import Decimal
from typing import Any

RANK = {"critical": 0, "high": 1, "medium": 2, "info": 3}
WEIGHTS = {
    "critical": Decimal("0.30"),
    "high": Decimal("0.15"),
    "medium": Decimal("0.08"),
    "info": Decimal("0.01"),
}


def review_priority(findings: list[Any]) -> dict[str, Any]:
    remaining = Decimal("1")
    for finding in findings:
        severity = finding["severity"] if isinstance(finding, dict) else finding.severity
        remaining *= 1 - WEIGHTS[severity]
    score = int(((1 - remaining) * 100).quantize(Decimal("1")))
    band = (
        "low" if score < 20 else "moderate" if score < 40 else "elevated" if score < 65 else "high"
    )
    return {
        "score": score,
        "band": band,
        "calibrated": False,
        "meaning": "Heuristic review priority; not a probability or investment risk rating.",
    }
