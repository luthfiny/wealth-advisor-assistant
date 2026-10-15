import threading
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import Mock

from tests.helpers import profile_raw
from wealth_advisor.domain import ValidationError
from wealth_advisor.mock_crm import create_server
from wealth_advisor.tools import HttpCRMTool, ToolError, ToolRegistry


@contextmanager
def running_crm(failure_mode="none"):
    server = create_server({"client_demo_001": profile_raw()}, port=0, failure_mode=failure_mode)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ToolTests(unittest.TestCase):
    def test_transient_failure_retries_then_succeeds(self):
        tool = Mock()
        tool.execute.side_effect = [
            ToolError("busy", "busy", retryable=True),
            ToolError("busy", "busy", retryable=True),
            "ok",
        ]
        sleeper = Mock()
        registry = ToolRegistry({"test": tool}, sleeper=sleeper)
        self.assertEqual(registry.call("test", {}, run_id="test"), "ok")
        self.assertEqual(tool.execute.call_count, 3)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [0.05, 0.1])

    def test_permanent_failure_does_not_retry(self):
        tool = Mock()
        tool.execute.side_effect = ToolError("not_found", "missing")
        with self.assertRaises(ToolError):
            ToolRegistry({"test": tool}).call("test", {}, run_id="test")
        self.assertEqual(tool.execute.call_count, 1)

    def test_retries_are_bounded(self):
        tool = Mock()
        tool.execute.side_effect = ToolError("busy", "busy", retryable=True)
        with self.assertRaises(ToolError):
            ToolRegistry({"test": tool}, sleeper=Mock()).call("test", {}, run_id="test")
        self.assertEqual(tool.execute.call_count, 3)

    def test_unexpected_exception_is_sanitized(self):
        tool = Mock()
        tool.execute.side_effect = RuntimeError("sensitive raw provider response")
        with self.assertRaises(ToolError) as caught:
            ToolRegistry({"test": tool}).call("test", {}, run_id="test")
        self.assertEqual(caught.exception.code, "tool_internal_error")
        self.assertNotIn("sensitive", str(caught.exception))

    def test_unknown_tool_is_explicit(self):
        with self.assertRaises(ToolError) as caught:
            ToolRegistry({}).call("absent", {}, run_id="test")
        self.assertEqual(caught.exception.code, "unknown_tool")

    def test_real_http_profile(self):
        with running_crm() as url:
            profile = HttpCRMTool(url).execute(
                {"client_id": "client_demo_001", "as_of": date(2026, 10, 1)}
            )
        self.assertEqual(profile.risk_tolerance, "moderate")

    def test_real_http_404_is_not_retryable(self):
        with running_crm() as url, self.assertRaises(ToolError) as caught:
            HttpCRMTool(url).execute({"client_id": "absent", "as_of": date(2026, 10, 1)})
        self.assertEqual(caught.exception.code, "crm_http_404")
        self.assertFalse(caught.exception.retryable)

    def test_real_http_outage_is_retryable(self):
        with running_crm("unavailable") as url, self.assertRaises(ToolError) as caught:
            HttpCRMTool(url).execute({"client_id": "client_demo_001", "as_of": date(2026, 10, 1)})
        self.assertTrue(caught.exception.retryable)

    def test_real_http_invalid_schema_is_not_retried(self):
        with running_crm("malformed") as url:
            registry = ToolRegistry({"crm": HttpCRMTool(url)})
            with self.assertRaises(ToolError) as caught:
                registry.call(
                    "crm",
                    {"client_id": "client_demo_001", "as_of": date(2026, 10, 1)},
                    run_id="test",
                )
        self.assertEqual(caught.exception.code, "invalid_data")
        self.assertFalse(caught.exception.retryable)

    def test_timeout_is_classified_as_transient(self):
        tool = HttpCRMTool("http://127.0.0.1:1", timeout=0.1)
        tool._opener = Mock()
        tool._opener.open.side_effect = TimeoutError()
        with self.assertRaises(ToolError) as caught:
            tool.execute({"client_id": "client_demo_001", "as_of": date(2026, 10, 1)})
        self.assertTrue(caught.exception.retryable)

    def test_invalid_http_configuration_is_rejected(self):
        for url in [
            "file:///tmp/crm",
            "http://user:secret@localhost",
            "http://",
            "ftp://example.org",
        ]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                HttpCRMTool(url)
        for timeout in [0, -1, 31, float("nan"), float("inf")]:
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                HttpCRMTool("http://127.0.0.1", timeout=timeout)


if __name__ == "__main__":
    unittest.main()
