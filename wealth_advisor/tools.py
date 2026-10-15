"""Ports, tool execution policy, and replaceable file/CRM/analytics adapters."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .analysis import DetectorRegistry
from .domain import ClientData, CRMProfile, ValidationError, loads
from .memory import Memory, MemoryError
from .metrics import AnalysisContext
from .normalize import prepare_input
from .policy import AnalysisPolicy
from .tool_runtime import (
    ToolClient as ToolClient,
)
from .tool_runtime import (
    ToolError as ToolError,
)
from .tool_runtime import (
    ToolRegistry as ToolRegistry,
)


def read_json(path: Path, *, max_bytes: int = 10_000_000) -> Any:
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ToolError("input_too_large", "JSON file exceeds the size limit")
        return loads(payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("input_unreadable", "Cannot read a valid JSON file") from exc


class FinancialDataTool:
    def execute(self, arguments: dict[str, Any]) -> ClientData:
        return ClientData.parse(read_json(Path(arguments["path"])))


class InputPreparationTool:
    """Validate imports and return an explicit data-quality record."""

    def __init__(self, mode="strict"):
        self.mode = mode

    def execute(self, arguments):
        return prepare_input(read_json(Path(arguments["path"])), self.mode)


class FixtureCRMTool:
    def __init__(self, path: Path, *, simulate_failure: bool = False):
        self._path = path
        self._simulate_failure = simulate_failure

    def execute(self, arguments: dict[str, Any]) -> CRMProfile:
        if self._simulate_failure:
            raise ToolError("crm_unavailable", "Simulated CRM outage", retryable=True)
        profiles = read_json(self._path)
        if not isinstance(profiles, dict):
            raise ToolError("crm_invalid_response", "CRM fixture must be an object")
        raw = profiles.get(arguments["client_id"])
        if raw is None:
            raise ToolError("crm_not_found", "Client does not exist in CRM")
        return CRMProfile.parse(raw, arguments["client_id"], arguments["as_of"])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


class HttpCRMTool:
    """Small real HTTP adapter with timeout, bounded reads and explicit retry semantics."""

    supports_deadline = True

    def __init__(self, base_url: str, *, timeout: float = 2.0):
        parts = urlsplit(base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValidationError(
                "CRM URL must be an HTTP(S) base URL without credentials or query"
            )
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 30:
            raise ValidationError("CRM timeout must be greater than zero and at most 30 seconds")
        self._url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def execute(self, arguments: dict[str, Any]) -> CRMProfile:
        request = Request(
            self._url + "/clients/" + quote(arguments["client_id"], safe=""),
            headers={"Accept": "application/json"},
        )
        try:
            deadline = arguments.get("_deadline", time.monotonic() + self._timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError("crm_timeout", "CRM deadline exhausted", retryable=True)
            with self._opener.open(request, timeout=min(self._timeout, remaining)) as response:
                chunks, size = [], 0
                while size <= 65536:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ToolError("crm_timeout", "CRM deadline exhausted", retryable=True)
                    native_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None), "_sock", None
                    )
                    if native_socket is not None:
                        native_socket.settimeout(min(self._timeout, remaining))
                    chunk = response.read1(min(8192, 65537 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                payload = b"".join(chunks)
                if len(payload) > 65_536:
                    raise ToolError("crm_invalid_response", "CRM response is too large")
                raw = loads(payload)
        except HTTPError as exc:
            transient = exc.code in {408, 429, 500, 502, 503, 504}
            exc.close()
            raise ToolError(
                f"crm_http_{exc.code}", "CRM returned an HTTP error", retryable=transient
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ToolError("crm_unavailable", "CRM request failed", retryable=True) from exc
        except (ValueError, UnicodeError) as exc:
            raise ToolError("crm_invalid_response", "CRM returned invalid JSON") from exc
        return CRMProfile.parse(raw, arguments["client_id"], arguments["as_of"])


class AnalyticsTool:
    def __init__(self, registry=None, policy=None, selected=None):
        self.registry = registry or DetectorRegistry()
        self.policy = policy or AnalysisPolicy()
        self.selected = selected

    def execute(self, arguments: dict[str, Any]) -> Any:
        context = arguments.get("context") or AnalysisContext(
            arguments["data"], arguments["profile"], arguments["history"], self.policy
        )
        return self.registry.run(context, arguments.get("selected", self.selected))


@dataclass
class CatalogTool:
    registry: Any

    def execute(self, arguments):
        return self.registry.catalog()


@dataclass
class AnalysisPreparationTool:
    policy: Any

    def execute(self, arguments):
        return AnalysisContext(
            arguments["data"], arguments["profile"], arguments["history"], self.policy
        )


@dataclass
class MemoryTool:
    memory: Any
    operation: str

    def execute(self, arguments):
        try:
            return getattr(self.memory, self.operation)(**arguments)
        except MemoryError as exc:
            raise ToolError("memory_unavailable", "Memory operation failed") from exc


@dataclass
class HistoryTool:
    """Accept a memory port; the tool does not depend on SQLite."""

    reader: Memory

    def execute(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.reader.history(
                arguments["client_id"], arguments["currency"], arguments["as_of"].isoformat()
            )
        except MemoryError as exc:
            raise ToolError("memory_unavailable", "Cannot read historical memory") from exc
