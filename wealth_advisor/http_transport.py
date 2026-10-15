"""Bounded HTTP reads shared by adapters; service schemas and errors stay with callers."""

import time
from typing import Any
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


class DeadlineExceeded(TimeoutError):
    """The shared monotonic request budget expired."""


class ResponseTooLarge(ValueError):
    """The response exceeded its configured byte limit."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


def read_http_response(
    request: Request,
    *,
    timeout: float,
    deadline: float | None = None,
    max_bytes: int = 65_536,
    opener: OpenerDirector | None = None,
) -> bytes:
    """Read at most max_bytes, block redirects, and close every successful response.

    Socket timeouts use the remaining budget before each read. urllib's socket
    access is isolated here; DNS and synchronous OS operations remain cooperative.
    HTTP errors propagate to adapters, which close them and map service semantics.
    """
    deadline = time.monotonic() + timeout if deadline is None else deadline
    opener = build_opener(_NoRedirect()) if opener is None else opener

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("HTTP request deadline exhausted")
        return min(timeout, remaining)

    with opener.open(request, timeout=remaining_timeout()) as response:
        chunks, size = [], 0
        while True:
            read_timeout = remaining_timeout()
            native_socket = getattr(
                getattr(getattr(response, "fp", None), "raw", None), "_sock", None
            )
            if native_socket is not None:
                native_socket.settimeout(read_timeout)
            chunk = response.read1(min(8192, max_bytes + 1 - size))
            remaining_timeout()
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge("HTTP response exceeds the size limit")
            chunks.append(chunk)
