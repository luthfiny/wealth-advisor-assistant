"""Verified sentence rendering with optional Claude selection and a deterministic fallback.

The model sees only sentence IDs, rule codes and severity labels. Financial values,
names, freeform transaction labels and reviewer notes stay local. Model-written text
never appears in the report: only approved sentence IDs can select local sentences.
"""

import os
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .domain import dumps, loads
from .http_transport import ResponseTooLarge, read_http_response
from .priority import review_priority
from .tool_runtime import ToolError


def sentence_catalog(brief):
    choices = []
    for finding in brief["findings"]:
        choices.append(
            {
                "id": finding["id"],
                "code": finding["code"],
                "severity": finding["severity"],
                "text": finding["title"] + ". " + finding["explanation"],
                "citations": [finding["id"]],
            }
        )
    return choices


def render_brief(brief, selected=None, source="template"):
    catalog = sentence_catalog(brief)
    by_id = {item["id"]: item for item in catalog}
    if selected is None:
        selected = [item["id"] for item in catalog[:3]]
    if (
        not isinstance(selected, list)
        or len(selected) > 3
        or any(not isinstance(i, str) for i in selected)
        or len(set(selected)) != len(selected)
        or not set(selected) <= by_id.keys()
    ):
        raise ToolError("narrative_invalid_selection", "Model selected unsupported sentence IDs")
    metrics = brief["metrics"]
    priority = review_priority(brief["findings"])
    parts = [
        {
            "text": (
                f"Recorded net worth is {metrics['currency']} "
                f"{Decimal(str(metrics['net_worth'])):,.2f}."
            ),
            "citations": ["metrics.net_worth"],
        }
    ]
    coverage = metrics.get("cash_runway_months")
    if coverage is not None:
        parts.append(
            {
                "text": f"Cash covers {coverage} months of declared expenses.",
                "citations": ["metrics.cash_runway_months"],
            }
        )
    parts.append(
        {
            "text": (
                f"Review priority is {priority['score']}/100 ({priority['band']}), "
                "an uncalibrated triage heuristic."
            ),
            "citations": [
                "reviewed_priority" if brief.get("status") == "approved" else "review_priority"
            ],
        }
    )
    parts.extend({"text": by_id[i]["text"], "citations": by_id[i]["citations"]} for i in selected)
    if brief.get("actions"):
        parts.append(
            {
                "text": "Proposed next step: " + brief["actions"][0]["instruction"],
                "citations": [brief["actions"][0]["id"]],
            }
        )
    if brief.get("warnings"):
        parts.append(
            {
                "text": (
                    "Some inputs or checks were limited; inspect the coverage and warnings "
                    "before making decisions."
                ),
                "citations": ["warnings", "skipped_checks"],
            }
        )
    parts.append(
        {"text": "Signals request verification and do not establish wrongdoing.", "citations": []}
    )
    if brief.get("status", "pending_review") == "pending_review":
        parts.append(
            {
                "text": "Recommendations remain a draft awaiting human approval.",
                "citations": ["status"],
            }
        )
    return {
        "text": " ".join(p["text"] for p in parts),
        "source": source,
        "sentences": parts,
        "selected_finding_ids": selected,
    }


class TemplateNarrativeTool:
    """Render an advisor briefing exclusively from verified metrics, findings and actions."""

    def execute(self, arguments):
        return render_brief(arguments["brief"])


class ClaudeNarrativeTool:
    """Select up to three verified sentence IDs using the Anthropic Messages API."""

    supports_deadline = True

    def __init__(self, settings, opener=None):
        self.settings = settings
        self.opener = opener

    def execute(self, arguments):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ToolError("narrative_unconfigured", "Anthropic credentials are not configured")
        choices = [
            {"sentence_id": item["id"], "topic": item["code"], "severity": item["severity"]}
            for item in sentence_catalog(arguments["brief"])
        ]
        payload = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": (
                "Select up to three provided sentence IDs for an advisor briefing, "
                'most urgent first. Return only JSON: {"sentence_ids": ["id"]}. Never invent IDs.'
            ),
            "messages": [{"role": "user", "content": dumps({"choices": choices}, indent=None)}],
        }
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            result = loads(
                read_http_response(
                    request,
                    timeout=self.settings.timeout_seconds,
                    deadline=arguments.get("_deadline"),
                    opener=self.opener,
                )
            )
        except ResponseTooLarge:
            raise ToolError(
                "narrative_invalid_response", "Narrative response exceeded size limit"
            ) from None
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ToolError(
                "narrative_http_error",
                "Narrative provider returned an error",
                retryable=status in {429, 500, 502, 503, 504},
            ) from None
        except (URLError, OSError, TimeoutError):
            raise ToolError(
                "narrative_timeout", "Narrative request failed", retryable=True
            ) from None
        except ValueError:
            raise ToolError(
                "narrative_invalid_response", "Narrative provider returned invalid JSON"
            ) from None
        try:
            if result.get("stop_reason") != "end_turn":
                raise ValueError("Incomplete narrative response")
            text = "".join(b["text"] for b in result["content"] if b["type"] == "text")
            selection = loads(text)
            if not isinstance(selection, dict) or set(selection) != {"sentence_ids"}:
                raise ValueError("Invalid selection shape")
            return render_brief(arguments["brief"], selection["sentence_ids"], "claude_selection")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ToolError(
                "narrative_invalid_response", "Narrative response failed evidence validation"
            ) from None
