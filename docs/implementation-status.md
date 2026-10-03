# Implementation status

This repository was created from the supplied design documents. Their descriptions of earlier code were historical references: the supplied folder contained documentation only. The runnable implementation now lives under `src/`. This page describes actual scope and does not change the historical design records.

## Implemented locally

| Design part | Implementation | Local verification |
|---|---|---|
| A: entrance | Strict FastAPI schema, bearer demo identity, optional JWT issuer/audience/expiry/signature/group verification, owner isolation, limits | API auth, request bounds, session ownership tests |
| B: coordinator | LangGraph plan/read/assess/refine/write/check/finalize, at most one refinement, shared atomic call budget, two concurrent source calls | Golden question, budget and terminal-stream tests |
| C: readers | Jira/Slack bounded specialists, official MCP SDK stdio + authenticated Streamable HTTP, strict catalog/arguments, source eligibility, document retrieval | Real local wire-level tests and adversarial fixtures |
| D: evidence | Sanitized registry, version/date preservation, relevance and source diversity, explicit source failures and fixture conflict note | Source grounding, empty/partial and revision tests |
| E: writer | Deterministic extractive demo; optional structured OpenAI evidence selection | Every released claim must be an exact source excerpt |
| F: release | Citation membership, exact excerpt support, safe URLs, content checks, revocation checks before and after temporary context save | Unsafe draft emits zero answer bytes; mid-release revocation blocks |
| G: ingestion | Local/web/Drive connectors, located format parsing, isolated upload parsing, screening, chunking, batch embeddings, persistent hybrid index, atomic publication and revocation | Incremental, failed-update, stale-version, parsing and deletion tests |
| Temporary memory | In-memory owner-bound sessions, six turns, one-hour inactivity expiry, source revalidation on history/follow-up | Cross-user access and history-revocation tests |
| Operations | Safe activity metadata, health endpoints, dependency pins, CI, local container recipe, synthetic evaluation | See verification report |

Demo planning and source-tool choices are deterministic, so demo mode does **not** claim to be LLM reasoning. OpenAI mode uses model calls for planning, specialist decisions and writing, within the same code-enforced permissions and budgets. The writer is intentionally extractive: this makes the support gate enforceable without treating a heuristic or evaluator model as proof of entailment.

## Gap-register mapping

| Gaps | Local outcome | Remaining enterprise work |
|---|---|---|
| G01, G02 | Local/JWT auth boundary, request/plan/call bounds | Organization SSO approval and interactive sign-in integration |
| G03–G07 | Eligibility, deterministic screening, reviewed tools, checked release and excerpt grounding | Evaluated corporate DLP and semantic safety; arbitrary paraphrase verification |
| G08 | Source dates retained; relevance/recency ordering and known correction note | General temporal conflict resolution and domain authority rules |
| G09 | Answer cache disabled; release/history dependency checks | Distributed invalidation if an answer cache is added |
| G10–G12 | Transactional SQLite publication, generation fencing, complete-enumeration pruning | Distributed publication/restore qualification and source notification processing |
| G13, G14 | Content-free operational records, safe public errors, deadlines, cancellation and budgets | Enterprise audit sink and provider-specific rate-limit/circuit-breaker policies |
| G15 | Expiring owner-bound context, bounded memory | Durable encrypted checkpoints/recovery across replicas; no request replay yet |
| G16 | Fresh index and real MCP authentication/catalog readiness, bounded probes and five-second cache; recovery tested | Continuous live-provider freshness monitoring |
| G17 | Pinned Python dependencies, non-root API container, localhost demo profile | Container scans/digest pinning, TLS/secret management and approved production config |
| G18 | Real-protocol fake MCP, required document format and connector paths | Live Jira/Slack provider adapters and account approval; live Drive/provider integration verification |
| G19 | Process-local request admission and request-wide call ledger | Shared per-provider quotas and cross-replica admission |
| G20 | Protocol, ingestion, API/security tests, small synthetic golden suite | Representative real-data eval, load/chaos tests and restore drills |

`APP_ENV=production` fails startup rather than silently deploying fake identity and fixtures. This is an explicit boundary around the unresolved approvals and release gates in the supplied production reference, not a claim that those requirements have been satisfied.

## Known limits

- Jira and Slack are read-only synthetic sources. Fixture URLs use reserved example domains and do not open real company tickets.
- Google Drive requires an approved access token and explicit company-wide item allowlist. No account was connected during implementation.
- Regex secret/instruction screening is a useful local defense, not comprehensive PII classification or prompt-injection prevention. A malicious source cannot add tools or scopes even if its text passes screening.
- Local SQLite metadata/search is designed for small corpora and a single API instance. Optional search adapters retain authoritative metadata checks; they are not a benchmarked distributed system.
- Exact excerpt checks prove that text came from cited evidence, not that the original source was correct or that every source is current. Upstream changes remain subject to detection lag.
- Temporary conversations disappear at restart. The index persists. No raw provider records or document uploads are retained after processing, and no prompt/body trace exporter is enabled.
- Scanned PDF OCR and legacy `.doc` are not supported. Parser subprocesses have deadlines and data limits; they are not a full OS sandbox on Windows.
- No external paid model, real corporate source, container deployment or million-request capacity claim is implied by local tests.

