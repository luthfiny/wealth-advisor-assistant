"""Optional MCP contract tests; install .[mcp] to include them."""

import asyncio
import importlib.util
import tempfile
import unittest
from functools import wraps
from pathlib import Path

from tests.helpers import ROOT

HAS_MCP = importlib.util.find_spec("mcp") is not None


def protocol_test(test):
    @wraps(test)
    def run(self):
        from mcp import Client

        from wealth_advisor.mcp_server import create_mcp

        async def check():
            async with Client(create_mcp(self.backend)) as client:
                self.client = client
                await test(self)

        asyncio.run(check())

    return run


@unittest.skipUnless(HAS_MCP, "Optional MCP dependencies are not installed")
class MCPTests(unittest.TestCase):
    def setUp(self):
        from wealth_advisor.mcp_server import PluginBackend
        from wealth_advisor.observability import configure_logging

        configure_logging("ERROR")

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = PluginBackend(ROOT, Path(self.temp.name)).__enter__()
        self.addCleanup(self.backend.__exit__, None, None, None)

    async def call(self, name, args):
        result = await self.client.call_tool(name, args)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    @protocol_test
    async def test_catalog_schemas_and_annotations(self):
        tools = {t.name: t for t in (await self.client.list_tools()).tools}
        self.assertEqual(len(tools), 6)
        self.assertTrue(tools["get_report"].annotations.read_only_hint)
        self.assertFalse(tools["analyze_client"].annotations.read_only_hint)
        self.assertFalse(tools["record_review"].annotations.idempotent_hint)
        self.assertEqual(len((await self.call("list_demo_clients", {}))["clients"]), 4)
        self.assertEqual(len((await self.call("get_capabilities", {}))["checks"]), 18)
        for name, args in (("analyze_client", {"sample": "missing"}), ("unknown", {})):
            self.assertTrue((await self.client.call_tool(name, args)).is_error)

    @protocol_test
    async def test_analysis_exact_money_gate_and_trace(self):
        report = await self.call("analyze_client", {"sample": "original"})
        self.assertEqual(report["metrics"]["net_worth"], "182000.00")
        self.assertFalse(report["advisory_release_allowed"])
        self.assertEqual(await self.call("get_report", {"run_id": report["run_id"]}), report)
        trace = await self.call("get_run_trace", {"run_id": report["run_id"]})
        self.assertEqual(len(trace["events"]), 3)
        self.assertTrue(any(c["tool"] == "crm.profile" for c in trace["tool_calls"]))

    @protocol_test
    async def test_real_http_outage_uses_cache_and_blocks_portfolio(self):
        await self.call("analyze_client", {"sample": "original"})
        report = await self.call("analyze_client", {"sample": "original", "crm_mode": "outage"})
        self.assertEqual(report["sources"]["crm_source"], "cache")
        self.assertEqual(report["execution_status"], "degraded")
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])
        self.assertEqual(
            next(c for c in report["tool_calls"] if c["tool"] == "crm.profile")["attempts"], 3
        )

    @protocol_test
    async def test_errors_do_not_break_subsequent_calls(self):
        for name, args in (
            ("get_report", {"run_id": "missing"}),
            ("analyze_client", {"sample": "import_repairs"}),
        ):
            self.assertTrue((await self.client.call_tool(name, args)).is_error)
        report = await self.call("analyze_client", {"sample": "balanced"})
        self.assertEqual(report["findings"], [])

    @protocol_test
    async def test_repairs_remain_explicit_and_blocked(self):
        report = await self.call(
            "analyze_client", {"sample": "import_repairs", "ingestion_mode": "lenient"}
        )
        self.assertEqual(report["data_quality"]["records_dropped"], 2)
        self.assertFalse(report["guardrails"]["portfolio_discussion_allowed"])

    @protocol_test
    async def test_synthetic_review_is_persisted_and_cannot_be_replaced(self):
        report = await self.call("analyze_client", {"sample": "balanced"})
        args = {
            "run_id": report["run_id"],
            "decision": "reject",
            "reviewer": "Test fixture",
            "notes": "Synthetic protocol contract test",
        }
        reviewed = await self.call("record_review", args)
        self.assertEqual(reviewed["status"], "rejected")
        self.assertFalse(reviewed["advisory_release_allowed"])
        self.assertTrue((await self.client.call_tool("record_review", args)).is_error)
