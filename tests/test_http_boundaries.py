"""Exercise HTTP response boundaries using real sockets, including a real timeout."""

import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wealth_advisor.tools import HttpCRMTool, ToolError, ToolRegistry


@contextmanager
def response_server(payload: bytes, *, status: int = 200, delay: float = 0):
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
                self.wfile.write(payload)
            except ConnectionError:
                pass  # The timeout test deliberately closes the client socket first.

        def log_message(self, *args):
            pass

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
