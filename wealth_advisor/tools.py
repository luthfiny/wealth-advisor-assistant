"""Ports, tool execution policy, and replaceable file/CRM/analytics adapters."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .analysis import analyze
from .domain import ClientData, CRMProfile, ValidationError
from .memory import Memory, MemoryError
from .observability import emit


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Tool(Protocol):
    def execute(self, arguments: dict[str, Any]) -> Any: ...


class ToolClient(Protocol):
    def call(self, name: str, arguments: dict[str, Any], *, run_id: str) -> Any: ...


class ToolRegistry:
    """Bounded retries for transient failures only; tool payloads never enter logs."""

    def __init__(
        self,
        tools: dict[str, Tool],
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.05,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._tools = dict(tools)
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._sleep = sleeper

    def call(self, name: str, arguments: dict[str, Any], *, run_id: str) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("unknown_tool", "Requested tool is not registered")
        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            emit("tool.started", run_id=run_id, tool=name, attempt=attempt)
            try:
                result = tool.execute(arguments)
            except ValidationError as exc:
                emit(
                    "tool.failed",
                    level=logging.WARNING,
                    run_id=run_id,
                    tool=name,
                    attempt=attempt,
                    code="invalid_data",
                    retryable=False,
                )
                raise ToolError("invalid_data", str(exc)) from exc
            except ToolError as exc:
                emit(
                    "tool.failed",
                    level=logging.WARNING,
                    run_id=run_id,
                    tool=name,
                    attempt=attempt,
                    code=exc.code,
                    retryable=exc.retryable,
                )
                if not exc.retryable or attempt == self._max_attempts:
                    raise
                self._sleep(self._backoff * 2 ** (attempt - 1))
            except Exception as exc:
                emit(
                    "tool.failed",
                    level=logging.ERROR,
                    run_id=run_id,
                    tool=name,
                    attempt=attempt,
                    code="tool_internal_error",
                    retryable=False,
                )
                raise ToolError("tool_internal_error", "Tool failed unexpectedly") from exc
            else:
                emit(
                    "tool.completed",
                    run_id=run_id,
                    tool=name,
                    attempt=attempt,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                return result
        raise AssertionError("unreachable")


def read_json(path: Path, *, max_bytes: int = 10_000_000) -> Any:
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ToolError("input_too_large", "JSON file exceeds the size limit")
        return json.loads(payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("input_unreadable", "Cannot read a valid JSON file") from exc


class FinancialDataTool:
    def execute(self, arguments: dict[str, Any]) -> ClientData:
        return ClientData.parse(read_json(Path(arguments["path"])))


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
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read(65_537)
                if len(payload) > 65_536:
                    raise ToolError("crm_invalid_response", "CRM response is too large")
                raw = json.loads(payload)
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
    def execute(self, arguments: dict[str, Any]) -> Any:
        return analyze(arguments["data"], arguments["profile"], arguments["history"])


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
