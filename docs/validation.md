# Local validation

The subsequent native MCP plugin demonstration and its measured results are in
[plugin-validation.md](plugin-validation.md). That run passed 110 tests with the
optional SDK installed and exercised the plugin's tools directly in the existing chat.

Validated on 15 October 2026 after the detector, tool-runtime, feedback, configuration, batch and narrative changes. The repository remains local. The GitHub Actions workflow has not run remotely.

| Environment or check | Result |
| --- | --- |
| Ubuntu / WSL, Python 3.11.15 | 104 unit tests passed |
| Ubuntu / WSL, Python 3.12.3 | 104 unit tests passed |
| Windows, Python 3.12.4 | 104 unit tests passed |
| Fresh CLI / HTTP CRM processes, Linux and Windows | All 23 scenarios passed on each platform |
| Seeded corruption checks, Linux and Windows | All 800 cases passed on each platform, seed 20261015 |
| Unit-test statement coverage | 95% overall; 100% for original detectors, shared metrics and feedback matching |
| Ruff 0.17.0 | Lint and formatting passed |
| Bytecode compilation | Passed |
| Wheel build and clean installation | Installed without runtime dependencies; installed CLI ran outside the source tree |

Failure-path tests intentionally produce error logs. The unit suite uses temporary databases, ephemeral loopback servers and mocked provider transports. No external CRM or paid model call is required.

## Reproduce

From the repository root:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -q
python3 scripts/validate.py
python3 scripts/fuzz.py --cases 800 --seed 20261015
uvx ruff check .
uvx ruff format --check .
python3 -m compileall -q wealth_advisor scripts
uvx --from coverage coverage run --source=wealth_advisor -m unittest discover -s tests -q
uvx --from coverage coverage report -m
uv build --wheel --out-dir output/validated-wheel-v2
```

Windows uses `python` instead of `python3`. Use `scripts/validate.py --output-root PATH` with a Windows-local temporary directory when the source checkout is on a WSL network path. Linux was tested with both `.venv/bin/python` (3.11) and the system `python3` (3.12). Python 3.13 is an included CI target, not a locally verified environment.

The isolated wheel check installed `wealth_advisor_assistant-0.1.0-py3-none-any.whl` into a fresh environment with `--no-deps`, ran its console command from `/tmp`, and used absolute input, CRM, configuration, database and output paths. It produced eight findings, USD 182,000 net worth and a pending human-review gate. The imported module came from the new environment's `site-packages`.

## Process scenarios

`scripts/validate.py` launches new CLI processes and actual HTTP CRM server processes. It checks:

1. A normal draft with exact sample calculations, an atomic export and a blocked release gate.
2. Approval with a human action-text override while preserving original evidence and actions.
3. Inspection from another process returning the saved review.
4. A competing final review rejected without replacing the original decision.
5. A second draft eligible for rejection.
6. Rejection exposing no released actions.
7. An earlier snapshot stored for the same client.
8. Correct historical selection and a net worth decline finding.
9. Healthy HTTP CRM enrichment and the health endpoint.
10. HTTP 503 fallback after exactly three attempts.
11. Invalid CRM profile data rejected without a retry.
12. Missing financial input producing no advisory actions.
13. A protected output collision rejected before changing database bytes.
14. The complete 18-detector catalog.
15. Scoped tool capabilities that exclude CRM access from the advisor.
16. Effective settings loaded from the checked-in JSON configuration.
17. Unknown configuration fields rejected before execution.
18. A strict batch continuing after one invalid import.
19. A lenient batch with documented repairs, individual exports and conservative guards.
20. File-based approval and finding dismissal with an updated reviewed narrative.
21. Feedback reuse on later matching evidence while preserving raw findings and requiring a new review.
22. Missing provider credentials triggering an explicit template fallback.
23. A dated CRM cache fallback that keeps portfolio discussion blocked.

Each execution saves per-scenario JSON, JSONL logs and a `summary.json` under a unique output directory. The runner shuts down its mock server processes on completion. Exported results and validation databases remain ignored by Git.

## Corruption and regression coverage

The seeded run alternates 400 strict and 400 lenient requests. Mutations include missing fields, incorrect types, invalid money, extreme exponents, duplicate IDs, malformed timestamps, unsupported metadata, incomplete transactions and date boundaries. It yielded **475 expected validation failures and 325 completed drafts**, with no unexpected internal failures. All results remained JSON serializable and unreleased. Valid requests still completed after the corrupted requests. This is deterministic mutation coverage, not an exhaustive fuzzer or a security audit.

Targeted unit tests also validate:

- Exact decimal ingestion, rejection of duplicate JSON keys and nonfinite amounts, sub-cent rejection, zero-expense behavior, and same-currency historical comparisons.
- Correct new detector evidence for income decline, equal charges, timestamp bursts, overnight activity, new countries and repeated cash movements.
- Explicit skips when optional metadata is absent; failure isolation for crashed detectors and invalid detector contracts.
- Tool allowlist enforcement and structured denied/unknown results without execution; retry budgets, circuit recovery and bounded concurrency without detached workers.
- Preservation of backend failure counts during local capacity contention.
- Atomic concurrent reviews; immutable originals; severity annotations; evidence and policy changes; expiry; date/client/currency scope; and continued review gating after suppression.
- Cache age, future observation exclusion and no cache fallback for not-found responses.
- Strict/lenient import behavior, repair provenance and fatal conflicting duplicate IDs.
- Mocked Claude request shape and privacy constraints, unsupported sentence IDs, duplicate selections, malformed/oversized responses, HTTP errors, transport failures and safe template fallback.
- Native HTTP socket timeouts, response caps, blocked redirects and consistency between fresh results and checked-in examples.

## Examples and limits

`scripts/generate_examples.py` rebuilds the original draft, reviewed, outage and historical examples using temporary SQLite stores. The outage example deliberately reuses a healthy earlier cache observation and labels that source. `scripts/generate_scenarios.py` creates three fictional clients and their lenient-mode reports: a balanced client with no findings, an activity case with 17 consolidated findings, and an import case with two dropped records and explicit data-quality issues.

No live financial feed, authenticated enterprise CRM, paid model response, trade execution or remote CI run was validated. The optional provider adapter was tested against mocks, and its generated content is restricted to selecting locally verified sentence IDs. Cooperative tool budgets do not forcibly terminate arbitrary Python code or guarantee DNS cancellation. These limits are also documented in the README.
