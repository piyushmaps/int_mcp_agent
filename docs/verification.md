# Verification results

Verified on October 3, 2026, using Python 3.12 on Windows.

| Check | Result |
|---|---|
| `python -m pytest -q` | **82 passed** |
| `python -m ruff check src tests scripts` | Passed |
| `node --check src/api/static/app.js` | Passed |
| Golden evaluation, `python -m src.eval.harness --fail-under 0.8 --baseline` | **8/8 passed**; documents-only baseline 7/8 |
| Desktop/mobile browser smoke, `python scripts/ui_smoke.py` | Passed against real local FastAPI and MCP flow |
| FAISS search parity | Passed with installed optional FAISS library |
| OpenSearch publication, authoritative retrieval and revocation | Passed against a controlled test transport; live cluster not exercised |
| Google Drive HTTP permission, allowlist and download behavior | Passed against mocked HTTP; no Google account connected |
| Dependency locks | Universal Windows/Linux/macOS environment markers generated |

The tests exercise real MCP stdio and authenticated Streamable HTTP initialization,
discovery and source reads. They cover request and tool bounds, source eligibility,
injection withholding, metadata redaction, citation/excerpt validation, safe event
ordering, output blocked before release, revocation before and after memory save,
history invalidation, owner isolation, ingestion persistence, failed updates,
stale revisions, document deletion, parser isolation, job leases and readiness
failure/recovery. Browser checks cover answers, citation previews, history,
upload/search/delete, activity and responsive layout.

Screenshots are generated under `artifacts/ui-desktop.png`, `ui-answer.png`,
`ui-knowledge.png`, `ui-mobile.png`, and `ui-mobile-knowledge.png`.
The synthetic evaluation details are in `data/eval/report.json`.

No paid OpenAI call, live Jira/Slack provider, live Google Drive account,
live OpenSearch deployment, Docker build/run, corporate identity provider,
cross-platform CI execution, production load test or operational restore drill
was performed. The eight-case golden set and its documents-only baseline measure
synthetic coverage; they are not evidence of general accuracy or production
capacity. See `implementation-status.md` for remaining enterprise release gates.

