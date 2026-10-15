"""Loopback HTTP CRM simulator, also used by integration tests."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .domain import dumps
from .observability import emit


def create_server(
    profiles: dict[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    failure_mode: str = "none",
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The demo CRM binds only to loopback")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if failure_mode not in {"none", "unavailable", "malformed"}:
        raise ValueError("Unsupported failure mode")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                status, payload = 200, {"status": "ok"}
            elif not path.startswith("/clients/"):
                status, payload = 404, {"error": "unknown_endpoint"}
            elif failure_mode == "unavailable":
                status, payload = 503, {"error": "simulated_outage"}
            elif failure_mode == "malformed":
                status, payload = 200, {"client_id": 17, "risk_tolerance": "unknown"}
            else:
                client_id = unquote(path[len("/clients/") :])
                payload = profiles.get(client_id)
                status = 200 if payload is not None else 404
                if payload is None:
                    payload = {"error": "client_not_found"}
            encoded = dumps(payload, indent=None).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            # Do not emit request paths, which include client identifiers.
            emit("mock_crm.request", method="GET")

    return ThreadingHTTPServer((host, port), Handler)
