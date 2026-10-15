"""Scoped tool capabilities and bounded synchronous execution without background workers."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import ValidationError
from .observability import emit


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    @property
    def kind(self):
        if self.code in {"tool_not_permitted", "unknown_tool"}:
            return "forbidden"
        if "timeout" in self.code or "deadline" in self.code:
            return "timeout"
        if "not_found" in self.code or self.code.endswith("404"):
            return "not_found"
        if self.code == "circuit_open":
            return "circuit_open"
        if self.retryable:
            return "transient"
        return "internal" if self.code == "tool_internal_error" else "invalid_input"


class Tool(Protocol):
    def execute(self, arguments: dict[str, Any]) -> Any: ...


class ToolClient(Protocol):
    def call(self, name: str, arguments: dict[str, Any], *, run_id: str) -> Any: ...


@dataclass(frozen=True)
class ToolPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.05
    deadline_seconds: float = 10.0
    circuit_threshold: int = 0
    circuit_cooldown_seconds: float = 30.0


@dataclass
class ToolResult:
    tool: str
    ok: bool
    value: Any = None
    error: ToolError | None = None
    attempts: int = 0
    duration_ms: float = 0.0

    def unwrap(self):
        if self.error is not None:
            raise self.error
        return self.value

    def metadata(self):
        return {
            "tool": self.tool,
            "ok": self.ok,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "error_code": self.error.code if self.error else None,
            "error_kind": self.error.kind if self.error else None,
        }


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    probing: bool = False
    lock: Any = field(default_factory=threading.Lock)

    def allow(self, policy, now):
        with self.lock:
            if not policy.circuit_threshold or self.opened_at is None:
                return True
            if now - self.opened_at < policy.circuit_cooldown_seconds or self.probing:
                return False
            self.probing = True
            return True

    def record(self, policy, now, error):
        with self.lock:
            self.probing = False
            if error is not None and error.code == "tool_busy":
                # Capacity contention says nothing about the backend's recovery.
                return
            if error is None or error.kind not in {"timeout", "transient", "internal"}:
                self.failures, self.opened_at = 0, None
            else:
                self.failures += 1
                if policy.circuit_threshold and self.failures >= policy.circuit_threshold:
                    self.opened_at = now


class ToolRegistry:
    def __init__(
        self,
        tools: dict[str, Tool],
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.05,
        sleeper: Callable = time.sleep,
        policies: dict[str, ToolPolicy] | None = None,
        max_in_flight: int = 4,
        clock: Callable = time.monotonic,
        default_policy: ToolPolicy | None = None,
    ):
        if max_attempts < 1 or max_in_flight < 1:
            raise ValueError("max_attempts and max_in_flight must be positive")
        self._tools = dict(tools)
        self._policies = policies or {}
        self._default = default_policy or ToolPolicy(
            max_attempts=max_attempts, backoff_seconds=backoff_seconds
        )
        self._sleep, self._clock = sleeper, clock
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._circuits = {name: _Circuit() for name in tools}

    def describe(self):
        return [
            {"name": n, "description": (getattr(t, "__doc__", None) or type(t).__name__).strip()}
            for n, t in sorted(self._tools.items())
        ]

    def scoped(self, allowed, observer=None):
        if not set(allowed) <= self._tools.keys():
            raise ToolError("unknown_tool", "Agent allowlist contains an unregistered tool")
        return ScopedToolClient(self, frozenset(allowed), observer)

    def call(self, name, arguments, *, run_id):
        return self.invoke(name, arguments, run_id=run_id).unwrap()

    def invoke(self, name, arguments, *, run_id):
        if name not in self._tools:
            return ToolResult(
                name, False, error=ToolError("unknown_tool", "Requested tool is not registered")
            )
        started = self._clock()
        policy = self._policies.get(name, self._default)
        circuit = self._circuits[name]
        if not circuit.allow(policy, started):
            error = ToolError("circuit_open", "Backend circuit is open")
            emit("tool.circuit_open", run_id=run_id, tool=name)
            return ToolResult(name, False, error=error)
        deadline = started + policy.deadline_seconds
        error, value, attempts = None, None, 0
        for attempt in range(1, policy.max_attempts + 1):
            if self._clock() >= deadline:
                error = ToolError(
                    "tool_deadline", "Tool call exhausted its execution budget", retryable=True
                )
                break
            attempts = attempt
            emit("tool.started", run_id=run_id, tool=name, attempt=attempt)
            acquired = self._slots.acquire(blocking=False)
            try:
                if not acquired:
                    raise ToolError("tool_busy", "Tool concurrency limit reached", retryable=True)
                # Only transports which advertise deadline support receive this internal field.
                payload = dict(arguments)
                if getattr(self._tools[name], "supports_deadline", False) is True:
                    payload["_deadline"] = deadline
                value = self._tools[name].execute(payload)
                if self._clock() > deadline:
                    raise ToolError(
                        "tool_deadline", "Tool exceeded its execution budget", retryable=True
                    )
                error = None
            except ValidationError as exc:
                error = ToolError("invalid_data", str(exc))
            except ToolError as exc:
                error = exc
            except Exception as exc:
                error = ToolError("tool_internal_error", "Tool failed unexpectedly")
                emit(
                    "tool.exception",
                    level=logging.ERROR,
                    run_id=run_id,
                    tool=name,
                    error_type=type(exc).__name__,
                )
            finally:
                if acquired:
                    self._slots.release()
            if error is None:
                emit(
                    "tool.completed",
                    run_id=run_id,
                    tool=name,
                    attempt=attempt,
                    duration_ms=round((self._clock() - started) * 1000, 2),
                )
                break
            emit(
                "tool.failed",
                level=logging.WARNING,
                run_id=run_id,
                tool=name,
                attempt=attempt,
                code=error.code,
                kind=error.kind,
                retryable=error.retryable,
            )
            if not error.retryable or attempt == policy.max_attempts:
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(policy.backoff_seconds * 2 ** (attempt - 1), remaining))
        circuit.record(policy, self._clock(), error)
        return ToolResult(
            name,
            error is None,
            value if error is None else None,
            error,
            attempts,
            round((self._clock() - started) * 1000, 2),
        )


class ScopedToolClient:
    def __init__(self, registry, allowed, observer=None):
        self.registry, self.available, self.observer = registry, allowed, observer

    def call(self, name, arguments, *, run_id):
        result = self.invoke(name, arguments, run_id=run_id)
        return result.unwrap()

    def invoke(self, name, arguments, *, run_id):
        if name not in self.available:
            result = ToolResult(
                name,
                False,
                error=ToolError("tool_not_permitted", "Tool is outside this agent's allowlist"),
            )
            emit("tool.denied", run_id=run_id, tool=name)
        else:
            result = self.registry.invoke(name, arguments, run_id=run_id)
        if self.observer:
            self.observer(result.metadata())
        return result
