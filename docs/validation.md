# Local validation

Revalidated on 15 October 2026. These are local results; the repository remains local and the included GitHub Actions workflow has not been run remotely.

| Environment | Result |
| --- | --- |
| Ubuntu / WSL, Python 3.11.15 | 70 tests passed |
| Ubuntu / WSL, Python 3.12.3 | 70 tests passed |
| Windows, Python 3.12.4 | 70 tests passed |
| Actual CLI / CRM processes, Linux and Windows | All 13 scenarios passed on each platform |
| Unit-test statement coverage | 94% overall; 100% for specialized agents and analysis |
| Ruff 0.17.0 | Lint and formatting checks passed |
| Editable package installation | Installed with no runtime dependencies; console entry point produced a pending draft |
| Wheel build and clean installation | Installed in an isolated environment; CLI ran from `/tmp`, outside the source tree |
| Python bytecode compilation | Passed |

Commands used from the repository root:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -q
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q
python3 scripts/validate.py
uvx ruff check .
uvx ruff format --check .
python3 -m compileall -q wealth_advisor scripts
uvx --from coverage coverage run --source=wealth_advisor -m unittest discover -s tests -q
uvx --from coverage coverage report -m
uv build --wheel --out-dir output/validation-wheel-final
uv pip install --python output/wheel-env/bin/python --no-deps --reinstall output/validation-wheel-final/wealth_advisor_assistant-0.1.0-py3-none-any.whl
```

The wheel smoke test used absolute input, fixture, database and output paths while running the installed console command from `/tmp`. It produced a completed analysis with eight findings, USD 182,000 net worth, and a pending review gate.

Windows used `python -W error::ResourceWarning -m unittest discover -s tests -q` and `python scripts/validate.py --output-root PATH` with a local temporary output directory. The unit suite starts ephemeral loopback HTTP servers and uses temporary databases, including concurrent review transactions. It does not call external services. Error log events during failure-path tests are expected.

## Process-level checks

`scripts/validate.py` launches fresh CLI processes and actual HTTP CRM server processes. It validates:

1. A normal draft, exact sample calculations, an atomic JSON export, and a blocked release gate.
2. Approval with a human action-text override, preserving original actions and financial evidence.
3. Inspection from a new process, returning the persisted approved result.
4. A duplicate review returning exit 2 without replacing the original decision.
5. A second draft for rejection.
6. Rejection keeping release blocked and exposing no reviewed actions.
7. An earlier-dated snapshot stored for the same client.
8. Selection of that snapshot and detection of the net worth decline.
9. A healthy HTTP CRM, including its health endpoint and profile enrichment.
10. HTTP 503 fallback after exactly three attempts, with portfolio discussion blocked.
11. An invalid CRM profile rejected after one attempt, with an explicit limited draft.
12. Missing financial input returning exit 1 and no advisory actions.
13. An output path equal to the database rejected before changing database bytes.

Each run saves per-scenario JSON and JSONL event logs plus `summary.json` under a unique directory. The Linux result for this validation is `output/validation/20261015T035929473482/summary.json`. Windows artifacts were written to a local temporary directory. The runner stops its server processes on completion.

## Fixes confirmed by regression tests

- Numeric JSON amounts previously passed through binary floating point: `999999999999999.99` became `1000000000000000.0`. External JSON now parses numbers directly as `Decimal`; sub-cent values are rejected without float rounding.
- Duplicate JSON fields previously overwrote earlier values. The parser now rejects them, including duplicate fields in HTTP CRM responses.
- A zero amount with an extreme exponent could create an excessively long JSON value. Monetary values now normalize to exact cents before serialization.
- `--output` could replace input or database files. Path validation now checks direct paths and aliases before analysis or review mutates state. A regression confirms a rejected review export leaves the decision pending.
- Category median/MAD calculations repeated for each recent transaction. The analysis now computes a category's statistics once per run. A local 10,000-transaction check retained the same three findings and reduced the analysis stage from 2.055 seconds to 0.003 seconds. This single local measurement excludes parsing, API access and persistence; it is not a latency guarantee.
- The wheel build now uses SPDX license metadata, removing the deprecated license-table configuration.

Additional tests cover real socket timeout retries, response-size caps, blocked redirects, large flat histories, and consistency between fresh financial results and the checked-in sample output.

Sample artifacts were generated by the running implementation, using temporary memory databases and synthetic fixtures. A fresh draft has eight findings and remains blocked for human review. The outage example has explicit missing-CRM context and blocked portfolio discussion. The reviewed example preserves original actions and records a synthetic reviewer override. The historical example compares the current snapshot with an earlier dated snapshot.

No live LLM, live financial data, authenticated CRM, trade execution, or remote CI run was tested. The default path does not implement those integrations. Python 3.13 remains a configured CI target rather than a locally verified environment.
