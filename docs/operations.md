# Local operations

## Start and stop

Run `python -m src` from the project root using the project's virtual environment. The launcher binds `127.0.0.1:8000`; Ctrl+C stops it. `scripts/start.ps1` and `scripts/start.sh` install pinned dependencies if the virtual environment is missing. `/health/live` answers without credentials; detailed health requires authentication.

The default MCP clients spawn official-SDK child servers per request branch. They close on completion/cancellation. For long-running server exercises use the HTTP commands in the README. They bind localhost, validate a local bearer token, and expose only the reviewed read tools. Do not expose these fake servers on a public interface.

## Persisted and temporary data

- `data/runtime/documents.sqlite`: approved sanitized document versions, vector data, authoritative serving blocks, and optional ingestion jobs/checkpoints. SQLite uses WAL and full synchronous transactions.
- OS/application temporary files: upload content while extraction runs; removed after processing. Failed or unsafe content is reported without returning parser exception text.
- Process memory: bounded temporary sessions and content-free activity metadata. Sessions expire after inactivity and disappear on restart.
- `data/eval/`: synthetic questions and measured synthetic evaluation output only.

Deleting a document writes a serving block and invalidates dependent sessions. The seeded demo does not automatically resurrect a blocked record when the app restarts. Do not remove tombstones during backup or migration.

## Backup and recovery

Stop the application and ingestion processes before copying the data directory, or use SQLite's online backup API to make a consistent copy. Back up the complete authoritative database rather than only vector rows. Test restoring to a separate directory, start the app with `DATA_DIR` pointing there, and confirm known revoked IDs remain unavailable. Current upstream permission changes must be reconciled before restoring real-data service; backups alone cannot establish current access.

This project has no measured RTO/RPO or automatic cross-replica recovery. The tests exercise atomic publication and job-lease recovery locally; they are not a full operational restore drill.

## Troubleshooting

| Symptom | Action |
|---|---|
| 401 | Match the UI connection token with `DEMO_TOKEN`, or provide a valid configured JWT |
| 403 in demo mode | Use localhost; fake identity intentionally rejects remote clients |
| 429 | Allow current requests to finish; process-local admission bounds concurrency |
| Empty document index | Start with `BOOTSTRAP_DEMO=true` or ingest an approved directory |
| MCP partial/unavailable | For HTTP mode, start both servers and match `MCP_TOKEN`; otherwise verify the venv can import `mcp` |
| Session unavailable | The session expired, was deleted, belongs to another actor, or cited evidence changed; start a new conversation |
| Upload rejected | Check supported format, byte limit and whether the content contains unsafe instructions; corrupt/scanned PDFs do not silently become empty documents |
| Embedding configuration changed | Build a new index using the desired model and point both ingestion and retrieval to it |
| Output blocked | Evidence was revoked/changed or the draft failed citation/content support checks; no rejected draft text is exposed |

## Optional model calls

Set `MODEL_MODE=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL` and `APPROVED_MODEL_PROCESSING=true` only after approving provider processing for the content. Calls use structured schemas, no model tools on the answer writer, explicit output/input limits, a per-request shared token reservation and the overall request deadline. Provider request storage is disabled with `store=False`; that setting alone does not replace an organization-specific retention agreement.

API keys stay in configuration and are never placed in graph state or source prompts. Model refusals/malformed responses/provider failures produce a safe unavailable response. The demo does not silently switch to an external model.

## Production handoff

Review the original `docs/architecture/production-reference.md` and the concrete `implementation-status.md` matrix. Before enabling a production profile, implement and approve live source identity, company eligibility, managed secrets, contextual screening, retained-data encryption, retention, provider quotas, distributed session/limit storage, TLS, load tests and operational recovery. Local fake-token tests do not establish enterprise OAuth compliance.

