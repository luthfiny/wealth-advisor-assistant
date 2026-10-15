"""One portfolio-discussion decision shared by action preparation and report output."""

from .domain import PortfolioGuard, Session

REQUIRED_CHECKS = frozenset({"crm_context", "suitability", "concentration", "liquidity"})
BLOCKING_CODES = frozenset({"CRM_UNAVAILABLE", "STALE_PROFILE", "KYC_REVIEW"})


def evaluate_portfolio_guard(session: Session) -> PortfolioGuard:
    reasons = []
    if session.profile_source != "fresh":
        reasons.append("crm_not_fresh")
    if session.failed_checks:
        reasons.append("detector_failure")
    if session.data_quality.get("issues"):
        reasons.append("input_repairs")
    reasons.extend(
        "missing_check:" + name for name in sorted(REQUIRED_CHECKS - set(session.executed_checks))
    )
    # Feedback can suppress a finding, but cannot remove a guard from original evidence.
    reasons.extend(
        "finding:" + code
        for code in sorted({f.code for f in session.raw_findings} & BLOCKING_CODES)
    )
    return PortfolioGuard(not reasons, tuple(reasons))
