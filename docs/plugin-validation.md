# Native plugin validation

Validated on 15 October 2026 by calling the installed Wealth Advisor Assistant plugin's native tools from the existing Codex chat. This was an actual MCP integration, including real HTTP calls to the loopback CRM simulator.

The local plugin is installed and enabled as `wealth-advisor@wealth-advisor-local`. Its tool namespace appeared during the session after installation; no app restart was needed in this observed run. Other sessions may need a refresh or restart if tools do not load.

## Native results

| Sample / mode | Findings | Execution | Observed call time |
| --- | --- | --- | --- |
| original | 8 | completed | 95 ms |
| balanced | 0 | completed | 56 ms |
| activity | 17 | completed | 70 ms |
| import_repairs | 0 | degraded | 76 ms |
| outage | 8 | degraded | 243 ms |

Times are single observations measured around native tool dispatch. They exclude model thinking time and plugin installation, and are not performance guarantees.

The native demo made **12 calls** across tool discovery, analysis, saved-report retrieval, execution-trace retrieval and error probes. It verified 14 assertions. All five resulting drafts remain pending review, with no reviewer decision recorded.

The original report recorded USD 182,000.00 net worth and 2.00 months of declared expense coverage. All three agents completed. Nine internal tool calls succeeded; six detector checks explicitly skipped because their required inputs were unavailable. The saved report matched the analysis result exactly.

The activity sample emitted signals for income decline, duplicate charges, transaction velocity, overnight activity, new-country activity and repeated cash movements. The balanced sample had no findings. Lenient import recorded every repair and dropped two records; strict import returned an MCP error. The outage made exactly three HTTP attempts, reported its dated cache source, and kept portfolio discussion blocked. Missing-report errors did not break later valid requests.

## Transport and unit validation

The official SDK client separately exercised the actual server subprocess over stdio:

- Linux client: 20 calls passed; protocol 2026-07-28.
- Windows client using the exact installed manifest and WSL launch command: 20 calls passed; connection observed at 674.27 ms.
- Windows client using classic initialization: 11 calls passed; protocol 2025-11-25.
- The complete suite passed 110 tests with MCP dependencies installed, including six MCP-specific contract tests. Without the optional SDK, the 104 base tests passed and six MCP tests skipped.
- Ruff lint, formatting, bytecode compilation and Git whitespace checks passed.

The plugin uses deterministic agents and a template narrative. No paid LLM call or live financial service was involved. Review rejection was tested only with isolated unit-test data; the live demonstration drafts were not approved or rejected.

Native evidence is saved locally under `output/native-plugin-20261015T063543Z/` in `results.json` and `summary.json`. Protocol transcripts are under `output/live-plugin-*/`. The native run IDs are:

- original: `88109956-bb1b-4b66-8a4b-b53c378c401b`
- balanced: `4147dd21-d440-4a16-b170-40b80ef25fd5`
- activity: `30e0a11a-1d1d-40dc-936e-6a0cecdfec92`
- import_repairs: `c2e95bae-06d8-4b16-ac96-a9949af1851f`
- outage: `6aff1b6d-43b9-49ca-92c7-aea664df34b0`

See [plugin setup and prompts](plugin.md) to reproduce the integration. The current chat can call its native tools directly.
