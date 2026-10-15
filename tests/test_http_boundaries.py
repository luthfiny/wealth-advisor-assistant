"""Exercise HTTP response boundaries using real sockets, including a real timeout."""

import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from wealth_advisor.config import NarrativeSettings
from wealth_advisor.http_transport import DeadlineExceeded, ResponseTooLarge, read_http_response
from wealth_advisor.narrative import ClaudeNarrativeTool
from wealth_advisor.tools import HttpCRMTool, ToolError, ToolRegistry


@contextmanager
def response_server(payload: bytes, *, status: int = 200, delay: float = 0, chunk_delay: float = 0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if status == 302:
                    self.send_header("Location", "/should-not-be-followed")
                self.end_headers()
                if chunk_delay:
                    for offset in range(0, len(payload), 1024):
                        self.wfile.write(payload[offset : offset + 1024])
                        self.wfile.flush()
                        time.sleep(chunk_delay)
                else:
                    self.wfile.write(payload)
            except ConnectionError:
                pass  # The timeout test deliberately closes the client socket first.

        def log_message(self, *args):
            pass

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Join in-flight test requests on close, including the deliberately delayed response.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class HTTPBoundaryTests(unittest.TestCase):
    arguments = {"client_id": "client_demo_001", "as_of": date(2026, 10, 1)}

    def test_invalid_json_is_a_permanent_crm_error(self):
        with response_server(b"not JSON") as (url, requests):
            with self.assertRaises(ToolError) as caught:
                ToolRegistry({"crm": HttpCRMTool(url)}).call("crm", self.arguments, run_id="test")
        self.assertEqual(caught.exception.code, "crm_invalid_response")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(requests), 1)

    def test_oversized_response_is_bounded_and_not_retried(self):
        with response_server(b" " * 70_000) as (url, requests):
            with self.assertRaises(ToolError) as caught:
                ToolRegistry({"crm": HttpCRMTool(url)}).call("crm", self.arguments, run_id="test")
        self.assertEqual(caught.exception.code, "crm_invalid_response")
        self.assertEqual(len(requests), 1)

    def test_redirects_are_not_followed(self):
        with response_server(b"{}", status=302) as (url, requests):
            with self.assertRaises(ToolError) as caught:
                ToolRegistry({"crm": HttpCRMTool(url)}).call("crm", self.arguments, run_id="test")
        self.assertEqual(caught.exception.code, "crm_http_302")
        self.assertEqual(requests, ["/clients/client_demo_001"])

    def test_duplicate_json_fields_in_api_response_are_rejected(self):
        payload = b'{"client_id":"other", "client_id":"client_demo_001"}'
        with response_server(payload) as (url, requests):
            with self.assertRaises(ToolError) as caught:
                ToolRegistry({"crm": HttpCRMTool(url)}).call("crm", self.arguments, run_id="test")
        self.assertEqual(caught.exception.code, "crm_invalid_response")
        self.assertEqual(len(requests), 1)

    def test_real_socket_timeout_retries_three_times_then_stops(self):
        with response_server(b"{}", delay=0.1) as (url, requests):
            registry = ToolRegistry({"crm": HttpCRMTool(url, timeout=0.02)})
            with self.assertRaises(ToolError) as caught:
                registry.call("crm", self.arguments, run_id="test")
        self.assertEqual(caught.exception.code, "crm_unavailable")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(requests), 3)


class SharedTransportTests(unittest.TestCase):
    def test_body_progress_cannot_extend_the_overall_deadline(self):
        with response_server(b"x" * 20_000, chunk_delay=0.01) as (url, requests):
            with self.assertRaises(TimeoutError):
                read_http_response(Request(url), timeout=1, deadline=time.monotonic() + 0.04)
        self.assertEqual(requests, ["/"])

    def test_exact_byte_limit_is_allowed_and_extra_byte_is_rejected(self):
        for size in (65_536, 65_537):
            with self.subTest(size=size), response_server(b"x" * size) as (url, _):
                if size == 65_536:
                    self.assertEqual(len(read_http_response(Request(url), timeout=1)), size)
                else:
                    with self.assertRaises(ResponseTooLarge):
                        read_http_response(Request(url), timeout=1)

    def test_expired_deadline_does_not_open_a_connection(self):
        opener = Mock()
        with self.assertRaises(DeadlineExceeded):
            read_http_response(Request("http://127.0.0.1/"), timeout=1, deadline=0, opener=opener)
        opener.open.assert_not_called()

    def test_response_closes_after_read_failure(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.fp = None
        response.read1.side_effect = TimeoutError("Synthetic read failure")
        with self.assertRaises(TimeoutError):
            read_http_response(Request("http://127.0.0.1/"), timeout=1, opener=opener)
        opener.open.return_value.__exit__.assert_called_once()

    def test_shared_transport_blocks_redirects(self):
        with response_server(b"{}", status=302) as (url, requests):
            with self.assertRaises(HTTPError) as caught:
                read_http_response(Request(url), timeout=1)
            caught.exception.close()
        self.assertEqual(requests, ["/"])

    def test_claude_adapter_uses_shared_transport_limits_and_error_mapping(self):
        # Replace only the endpoint, preserving real POST, socket, read and redirect handling.
        cases = [
            (b"{}", 302, 0, "narrative_http_error"),
            (b"x" * 65_537, 200, 0, "narrative_invalid_response"),
            (b"{}", 200, 0.1, "narrative_timeout"),
        ]
        for payload, status, delay, code in cases:
            with (
                self.subTest(code=code),
                response_server(payload, status=status, delay=delay) as (url, requests),
                patch(
                    "wealth_advisor.narrative.Request",
                    side_effect=lambda _, **kw: Request(url, **kw),
                ),
                patch.dict("os.environ", {"ANTHROPIC_API_KEY": "synthetic-test-key"}),
            ):
                tool = ClaudeNarrativeTool(NarrativeSettings(timeout_seconds=0.02 if delay else 1))
                with self.assertRaises(ToolError) as caught:
                    tool.execute({"brief": {"findings": []}})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(requests), 1)
