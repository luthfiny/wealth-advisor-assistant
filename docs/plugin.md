# Local Wealth Advisor plugin

See [the recorded native demonstration](plugin-validation.md) for verified results
and timings from using this plugin directly inside the existing Codex chat.

The optional MCP adapter exposes six tools: `list_demo_clients`, `get_capabilities`,
`analyze_client`, `get_report`, `get_run_trace` and `record_review`. It analyzes
only the four included synthetic samples. Its CRM calls use actual HTTP requests
to loopback mock servers; it does not contact real financial services or an LLM provider.

The adapter calls the existing orchestrator and storage layer. It preserves exact
money strings, detector coverage, retries, cache provenance and the review gate.
Unknown tools, invalid arguments, malformed imports and missing reports return MCP
errors without terminating the session. Reviewer decisions remain explicit, final
and audited. Never ask the host to approve a run without reviewing its evidence.

## Install the optional runtime

In the Linux checkout:

```bash
uv venv .venv-mcp --python 3.12
uv pip install --python .venv-mcp/bin/python -e '.[mcp]'
.venv-mcp/bin/python -m wealth_advisor.mcp_server --workspace "$PWD"
```

The last command runs an MCP stdio server, not a terminal REPL. A host must send
protocol requests to stdin. JSONL application logs go to stderr; stdout remains
reserved for the protocol. Servers and their owned CRM threads stop when the
MCP connection closes.

For a live standalone protocol session:

```bash
.venv-mcp/bin/python scripts/live_plugin_demo.py --outage
```

This uses the official SDK client to launch the actual server subprocess,
negotiate a protocol version, discover tools and make 20 tool calls. It runs all
four samples, checks saved reports and routing, simulates an HTTP outage, probes
invalid requests, and confirms drafts remain unreleased. Full requests and
results go to a new `output/live-plugin-*/transcript.json`; the summary records
timings and assertions. Timings are local observations, not latency guarantees.

## Install in the desktop app

Package a development plugin pointing at this checkout:

```bash
.venv-mcp/bin/python scripts/package_plugin.py --windows-wsl Ubuntu
```

For a Linux host, omit `--windows-wsl`. The builder generates a local marketplace
under `output/plugin-marketplace`. On Windows, use its UNC path:

```powershell
codex plugin marketplace add '\\wsl.localhost\Ubuntu\home\finsn\src\wealth-advisor-assistant\output\plugin-marketplace'
codex plugin add wealth-advisor@wealth-advisor-local
codex plugin list --marketplace wealth-advisor-local --json
```

Adjust the path and distro name to match your checkout. The plugin launch command
uses `wsl.exe` to run `.venv-mcp/bin/python` from that checkout. It needs no WSL port
forwarding or public tunnel. Plugin reports and cache persist in `data/plugin/memory.sqlite3`.

This is a development package: its installed manifest points to a local checkout
and optional environment. Keep both available. It is not a self-contained public
distribution. The source manifest is under `plugins/wealth-advisor`; the generated
manifest supplies the machine-specific launch command. Repackage and reinstall
after changing the manifest. Core Python edits are read from the checkout on the
next server start.

The [official plugin packaging documentation](https://developers.openai.com/plugins/build/plugins)
describes local marketplaces and restarting the desktop app after local plugin
changes. Installation does not prove an existing chat has loaded the new tools.
Restart the app, reopen the chat and select Wealth Advisor Assistant if its tools
are missing. This integration does not restart the app automatically.

Try these prompts after the plugin loads:

- “Use Wealth Advisor Assistant to analyze the original synthetic client and show its agent trace.”
- “Compare the balanced and activity samples. Explain each finding using its evidence.”
- “Simulate a CRM outage for the original sample and show retry attempts, cache provenance and guards.”
- “Analyze import_repairs in strict mode, then rerun with explicit lenient mode and explain every repair.”

Analysis creates drafts. `record_review` changes a draft's final review state and
should be called only for an explicitly requested decision on an identified run.
The demo runner never approves or rejects its live demonstration runs. The unit
suite tests rejection using isolated temporary data.

## Validate the protocol boundary

```bash
.venv-mcp/bin/python -W error::ResourceWarning -m unittest tests.test_mcp -v
.venv-mcp/bin/python scripts/live_plugin_demo.py --outage
```

To validate the exact installed manifest, run the demo from a Python environment
with `mcp==2.3.0` on the same OS as the desktop host and pass `--plugin-dir` with
the installed plugin's path. On Windows this exercises the complete
Windows client → `wsl.exe` → Linux MCP server → agents → loopback CRM path.

The [official MCP Python SDK documentation](https://py.sdk.modelcontextprotocol.io/client/)
defines the client and stdio transport used by this runner. MCP dependencies are
optional; the normal standard-library suite skips six protocol tests when the
SDK is absent. CI includes a separate plugin job that installs the extra and runs
the protocol tests and live demo.
