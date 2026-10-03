# Concrete gaps and closure checks

Reviewed September 29, 2026. All gaps below are **open**. Code observations are
read-only findings, not runtime/security audit results. Proposed controls are not
implemented by these documents. IDs appear in the workflow and service designs.

Priority: P0 = before real company data; P1 = before production pilot; P2 = before scale qualification.

| ID / priority | Current evidence or missing capability | Required change / owner | Acceptance check |
|---|---|---|---|
| G01 P0 | [API](../../src/api/main.py) has no application auth on question/cache administration routes | API identity and admin policy; bind sessions to actor | Unauthenticated calls fail; employee cannot clear cache or read another session |
| G02 P0 | `AskRequest` has minimum length only; [routing](../../src/agent/nodes.py) accepts unbounded subqueries and async gather starts all of them | API limits, schema-validated plan, central budget ledger, bounded dispatch | Oversized input rejected; 100-query plan cannot spawn 100 calls |
| G03 P0 | [Retriever](../../src/retrieval/retriever.py) filters source/relevance but has no eligibility enforcement | Policy gateway plus pre-search scope and post-result checks | Restricted, revoked and unknown-eligibility fixtures never reach models or responses |
| G04 P0 | [Ingestion](../../src/ingestion/pipeline.py) embeds document text without a sanitization gate; proposed MCP path needs the same boundary | Policy screening before embedding, model input, tracing and storage | Fake secret absent from embeddings, checkpoints, traces and output |
| G05 P0 | Current prompts embed evidence without a dedicated untrusted-data/tool boundary; MCP integration is not yet implemented | Approved tool/server registry, scoped credentials, argument/URL checks, read-only tools | Injected instructions cannot invoke writes, arbitrary URLs or new servers |
| G06 P0 | [Graph streaming](../../src/agent/graph.py) emits synthesis tokens immediately | Response release gate after draft validation; only safe progress events before release | A draft containing a secret yields zero draft bytes in SSE |
| G07 P1 | `_cited_only` in [nodes](../../src/agent/nodes.py) filters markers and falls back to all citations; it does not establish support | Evidence registry, citation ID/URL checks, claim-support assessment | Invented citation rejected; unsupported answer withheld or qualified |
| G08 P1 | Synthesis prompt prefers DevPortal over Slack regardless of dates; retrieved chunk lacks dedicated revision/time fields | Typed evidence and freshness-aware conflict handling | Newer relevant Slack conflict is shown with older documentation and both dates |
| G09 P0 | [Cache](../../src/api/cache.py) keys on questions/similarity and relies on TTL | Policy/evidence version dependencies, scope isolation, revocation recheck; semantic cache off initially | Revoked evidence immediately blocks reuse once revocation is recorded, before TTL expires |
| G10 P1 | `_Batcher.flush` deletes old chunks before replacement upsert | Versioned publication in ingestion; keep old eligible version until replacement ready | Injected write failure preserves previous eligible version |
| G11 P1 | Manifest commits precede some FAISS persistence checkpoints | Durable artifact first, metadata activation second, recovery reconciliation | Crash after each publication step neither loses committed knowledge nor serves uncommitted versions |
| G12 P0 | Pruning compares `seen` with manifest without a successful scope-enumeration contract; extraction failures are collected later | Scope-qualified enumeration completeness; failed items remain accounted for | Partial listing or corrupt file cannot delete unrelated/previous documents |
| G13 P0 | [Tracing](../../src/observability/tracing.py) records queries, titles, URLs and text previews when enabled | Sanitized event schemas and restricted audit pipeline | Secret fixture absent from logger/callback/exporter payloads |
| G14 P1 | Streaming sends `str(exc)`; no explicit end-to-end deadline/cancellation contract | Typed safe errors, source status, deadlines, bounded retries and cancellation | Upstream outage returns a safe terminal event without traceback/credentials |
| G15 P1 | Current graph compiles without checkpointer; request state has no session ownership/history | Expiring owner-bound session/run storage and replay rules | Two users cannot share state; resumed call rechecks policy and respects remaining budget |
| G16 P1 | `/health` uses a startup chunk count; retriever startup failure leaves no explicit recovery mechanism | Separate liveness/readiness, fresh dependency checks, bounded reconnection | Readiness reflects dependency recovery without exposing internal configuration |
| G17 P1 | [Compose](../../infra/docker-compose.yml) disables OpenSearch security for local development; [Dockerfile](../../infra/Dockerfile) lacks non-root USER; [dependencies](../../pyproject.toml) use lower bounds | Hardened deployment profile, reproducible lock, scanning, secrets, TLS, rollback | Release build reproducible; production rejects insecure configuration |
| G18 P1 | MCP and Drive/web/Word production integrations are not yet established by current sample-file paths | Integration contracts, real-protocol fake servers, supported-format matrix | Wire-level MCP tests and representative format fixtures; no unsupported connector advertised as ready |
| G19 P2 | Process-local cache and no cross-replica provider admission policy | Shared quotas/budgets, queue limits, overload handling and capacity tests | Multiple replicas together stay inside configured source quota |
| G20 P1 | Existing tests/eval exist, but new workflow controls have no verification yet | New security/quality datasets, chaos tests, restore tests, release gates | Publish actual results with failures and measured latency; no readiness claim from mock speed |

## Required failure fixtures

Use synthetic-only secrets/identities. Include normal results, empty results,
restricted items, missing eligibility, revoked documents, pagination, malformed
tool replies, prompt injection, sensitive URLs/titles, 429 responses with retry
hints, authentication expiry, slow/hung calls, partial source outages, duplicate
events, out-of-order revisions and crashes during publication.

For every fixture, specify allowed behavior and observable evidence. For example:
an output-leakage test checks all emitted events, not merely the final answer.

## New architecture risks to prevent

- A guardrail node is bypassable if any tool/model adapter can be called outside its gateway.
- Raw MCP responses must not be checkpointed while waiting for a later sanitization node.
- LangGraph state schemas do not themselves sanitize streamed graph events.
- Security revocations take precedence over cache hits, retries and restored snapshots.
- An additional evaluator model is fallible and cannot authorize access.
- A million-request claim requires a time unit, peak profile and actual downstream capacity.
