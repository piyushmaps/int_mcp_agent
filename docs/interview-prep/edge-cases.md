# Engineering Atlas — edge cases and interview answers

Reviewed at commit `bde0429`. Each edge case gives what happens, where it happens, how we know, and a short answer you can give in an interview.

**How we know** uses three labels:

- **Reproduced**: run against the real app (FastAPI TestClient, real stdio MCP servers, demo mode). The output is quoted.
- **Arithmetic**: worked out from the code's own constants. Not run against a paid API.
- **Code reading**: inferred from the code and not reproduced. Present these as risks, not as bugs you observed.

The test suite passes as of this review: `81 passed, 1 skipped`.

## Diagrams (made with Archify)

Open these files in a browser. Each one is standalone HTML with pan, zoom, path tracing, and source links that point back to this commit.

| File | Shows |
|---|---|
| [`docs/diagrams/architecture.html`](../diagrams/architecture.html) | Every runtime component, the process boundary, the MCP fixture servers, and the ingestion path into SQLite |
| [`docs/diagrams/query-workflow.html`](../diagrams/query-workflow.html) | One question moving through the LangGraph nodes, including the refinement loop and the two "blocked" exits |
| [`docs/diagrams/ingestion-dataflow.html`](../diagrams/ingestion-dataflow.html) | Screen before embedding, quarantine, and atomic publication |

The diagram sources are in `docs/diagrams/source/*.json`. To regenerate one, install Archify (`npx skills add tt-a1i/archify -g`) and run:

```bash
node <archify>/bin/archify.mjs finalize architecture docs/diagrams/source/architecture.json \
  docs/diagrams/architecture.html --repo-root . --quality showcase --json
```

Use `workflow` or `dataflow` as the type for the other two diagrams. For the browser gate, set `ARCHIFY_CHROME` to a Chrome or Chromium binary.

---

## The 60-second story

> A user asks an engineering question. FastAPI authenticates them, caps the request size, and admits it (12 requests globally, 2 per user). A LangGraph state machine **plans** which sources to read (Jira, Slack, documents). It then **reads them in parallel**. Jira and Slack go through real MCP servers, behind a client that pins the tool catalog, validates arguments against strict schemas, and screens every record. Documents come from a hybrid BM25 + vector index in SQLite. All of this shares one request budget: 8 tool calls, 2 at a time, 30 seconds. The graph then **assesses** the evidence, refines once if it found nothing, **writes** an answer from verbatim excerpts, and **validates** that every claim appears in the evidence it cites. Before release it **rechecks** that no cited source was revoked or changed, both before and after saving to memory. The browser only receives progress events, then one checked answer. Raw drafts are never streamed. On the ingestion side, content is screened *before* it is embedded. Injected content is quarantined, and publication is an atomic swap guarded by a generation number.

---

## A. Input screening and safety

### A1. Legitimate questions are rejected as prompt injection — *Reproduced*
- `"How do I disable the security scanner in CI?"` → **422 `question_failed_content_check`**
- `"Can we bypass access checks during an incident?"` → rejected
- **Where:** `src/policy/safety.py:10-15`. The pattern `(override|bypass|disable)\s+(the\s+)?(safety|security|access|policy|guardrail)` runs on the user's own question in `src/api/main.py:128`.
- **Interview answer:** "The injection regex is a tripwire, and it is tuned toward false positives. In an engineering tool, 'disable security' is a normal phrase. I would stop running the instruction detector on the authenticated user's question, because the user is not an untrusted third party. I would keep it on retrieved content. Even there it is defense in depth, not the main control."
- **Same root cause in ingestion:** a runbook containing "disable the security group rule" would quarantine the whole document (`src/ingestion/pipeline.py:131-138`).

### A2. Homoglyphs and paraphrases bypass the injection regex — *Reproduced*
- `"ignоre all previous instructions"` with a Cyrillic `о` → **allowed**. NFKC normalization does not fold confusable characters.
- **Interview answer:** "No regex will catch every injection. The real defense is architectural. Tools are read-only and allowlisted. Code, not the model, owns permissions and budgets. Every released claim must be a verbatim excerpt of cited evidence. So an injection that gets through can at worst change which true excerpts are chosen. It cannot make the model call a write tool or invent a fact."

### A3. Redaction silently changes the question — *Reproduced*
- `"What is the password: policy for Ledger?"` → `"What is the [REDACTED] for Ledger?"`
- `"Build 123-45-6789 failed"` → `"Build [REDACTED] failed"` (the SSN pattern)
- `"Contact alex@example.com about PAY-123"` → the email is redacted
- **Where:** `src/policy/safety.py:16-34`.
- **Interview answer:** "Redaction runs before anything reaches the model, the logs, or the index, which is correct for secrets. The trade-off is recall: the system now searches for a different question than the one asked. I would tell the user when the question was redacted, and use more specific secret detectors, such as entropy plus key prefixes, instead of `password:`."

### A4. Uppercase `AND` / `OR` / `NOT` in a question gets the Jira/Slack read denied — *Reproduced*
- `"Why is the release NOT shipped?"`: the first-round Slack and Jira reads fail `SearchArguments.plain_query`, so the branch status is `denied`.
- Refinement then drops words shorter than 4 letters, so `NOT` disappears and the second round succeeds by accident.
- **Second bug found while reproducing this:** `source_status` reports `{'jira': 'success', 'slack': 'success'}`, while `limitations` says "Slack could not be fully checked". The cause is `source_status={b.source: b.status for b in branches}` (`src/orchestration/graph.py:165`). That is last-write-wins across rounds, so the refinement round overwrites the first-round failure.
- **Where:** `src/integrations/mcp/catalog.py:23-29` and `src/agents/specialists.py:42-45`.
- **Interview answer:** "The argument validator blocks query-language operators so a model can't smuggle JQL. But the deterministic path builds the query from the user's raw words. The fix is to remove operator tokens when building the query, instead of rejecting the whole read. I'd also report the worst status per source, not the last one."

### A5. Body limit versus question limit — *Code reading*
- JSON bodies are capped at 40,000 bytes (`src/api/main.py:68`). Questions can be up to 8,000 characters. A client that escapes non-ASCII as `\uXXXX` (6 bytes per character) can get a 413 on a valid question. Browsers send raw UTF-8, so the UI is unaffected.

---

## B. Retrieval and matching

### B1. `PAY 123` (no hyphen) cites the wrong ticket — *Reproduced*
- `"What is the status of PAY 123?"` → status `partial`, citation **`PAY-124`**.
- **Why:** the Jira key regex needs a hyphen. Without one, the specialist does a keyword search, and `pay` matches every PAY issue, so the ranking picks another ticket.
- **Where:** `src/agents/specialists.py:38` and `src/integrations/mcp/repository.py:92-105`.
- **Fix:** normalize identifier-like patterns (`[A-Z]+\s*-?\s*\d+` → `KEY-N`) before planning.

### B2. Only the first ticket key is read — *Reproduced*
- `"Compare PAY-123 and PAY-124 status"` → plan = `jira` only, and the citations contain only `PAY-123`.
- **Where:** `src/agents/specialists.py:38-40` uses `keys[0]`.
- **Fix:** one `get_issue` per key, up to the budget, or a plan with one task per key.

### B3. Hash embeddings have no semantics
- `HashEmbeddings` is signed feature hashing (`src/ingestion/embeddings.py:24-46`). "postponed" does not match "delayed". The retriever also drops any hash-embedding hit that has zero exact term matches (`src/retrieval/retriever.py:41-42`). In demo mode retrieval is therefore effectively lexical, plus a hashed cosine score.
- **Answer:** "It's an offline demo choice so the project runs without API keys and gives repeatable results. Switching to `EMBEDDING_MODE=openai` needs a new index, which the fingerprint lock enforces."

### B4. Every query scans the whole index — *Code reading*
- `SQLiteVectorStore.search` loads **every active chunk**, JSON-decodes every vector, and computes cosine and BM25 in Python (`src/vectorstore/sqlite.py:208-225`). That is O(N) per query.
- **Answer:** "SQLite is the transactional authority for what is published. It isn't the ANN engine. The FAISS and OpenSearch adapters exist for scale, and SQLite stays the metadata authority so revocation is still checked against SQLite (`is_current`)."

### B5. A missing or typo'd Jira key has no fallback — *Code reading*
- `get_issue` returns `found=false`, and the next decision is `finish` (`src/agents/specialists.py:37-55`). No search is tried. Refinement retries the same key.

### B6. Wasted budget call after a Jira search — *Code reading*
- After `search_issues`, the specialist calls `get_issue` on the top hit (`specialists.py:46-47`). The search result already contains the full record, so this spends 1 of the 8 shared calls.

### B7. Two tokenizers disagree — *Code reading*
- `embeddings.tokens` keeps `pay-123` as one token. `writer.terms` splits it into `pay` and `123`. Retrieval and the assess/writer relevance filters therefore measure overlap differently.

---

## C. Evidence assessment and answer writing

### C1. Recency cap drops undated or older but relevant evidence — *Reproduced*
- `merge_evidence` sorts by `updated_at` descending and keeps **12** items (`src/evidence/checker.py:20-24`). Undated items use epoch, so they sort last.
- Test: 12 dated Slack messages plus 1 undated guide → **the guide is dropped**.
- Web connector documents always have `updated_at=None` (`src/connectors/web.py:100`), so they lose first.
- **Answer:** "I'd cap per source and rank by relevance, then show dates, instead of using one global recency sort."

### C2. Jira issue with an empty description is silently dropped — *Reproduced*
- Screening builds `f"Status: {s}. Team: {t}. {description}"`. With an empty description the text ends in a space. `merge_evidence` then rejects any item where `sanitize_text(text) != text`, and `sanitize_text` strips whitespace (`src/integrations/mcp/screening.py:109`, `src/evidence/checker.py:13`).
- **Answer:** this is a defense-in-depth check that treats "not idempotent" as "unsafe". The fix is to normalize whitespace when building evidence.

### C3. The claim gate is all-or-nothing and sensitive to syntax — *Reproduced*
- A claim containing `"errors are <1% for a minute and >5% ..."` → `validate_output` returns **False**, and the whole answer is `blocked`. The markup filter `<[^>]+>` matches `<1% ... >`.
- **Where:** `src/evidence/checker.py:35-44` and the `blocked` branch in `src/orchestration/graph.py:152-153`.
- In OpenAI mode, a single non-verbatim excerpt (smart quotes, a fixed typo) blocks the entire answer.
- **Answer:** "This is a deliberate choice for safety over availability. One unsupported claim withholds everything. The improvement is to drop failing claims and release the rest as `partial`, with a limitation noting what was dropped."

### C4. Conflict detection is keyword-based and tuned to the fixtures — *Code reading*
- `"blocked"/"delayed"` together with `"on track"/"ready to launch"`, and `"correction"` together with `"initial suspicion"` (`src/orchestration/graph.py:118-122`).
- **Answer:** "Be honest about this one. It's a placeholder for real conflict detection. The principle it demonstrates is correct: surface conflicts with dates and don't silently reconcile them. A production version would compare structured fields such as status and revision across sources, or use an LLM judge whose output only adds a limitation and never changes facts."

### C5. Status semantics — *Code reading*
- Any limitation at all, including the informational "later Slack correction supersedes...", turns `answered` into `partial` (`graph.py:160-161`).

### C6. Limits of the extractive writer — *Code reading*
- At most 2 sentences per source group (6 claims in total). Sentences of 20 characters or fewer are dropped unless they start with `Status:`. Sentence splitting on `. ` breaks on "e.g." and "v1.2." (`src/answering/writer.py:15-48`).

### C7. Follow-up questions pull in the previous topic — *Reproduced*
- Ask "Why is the Payments release delayed?", then in the same session "Who is the owner of incident response?"
- The resolved query is `"Who is the owner of incident response? Context from previous question: Why is the Payments release delayed?"`. The citations are **Payments release, #payments-release, Payments ownership**. The incident-response guide is **not** cited.
- **Why:** the trigger regex includes `owner|it|this|that|they` (`src/orchestration/planner.py:8-17`). It appends the previous question to any question of 14 words or fewer.
- **Answer:** "Only add context when the question has an unresolved reference and no entity of its own. The good property to point out: saved answers are never reused as evidence. Follow-ups always re-read sources."

---

## D. Budgets, deadlines, concurrency

### D1. OpenAI mode: tool decisions use up the writer's token budget — *Arithmetic*
- `ModelGateway` reserves `bytes(instruction + payload + schema) + 2048 + max_output_tokens` per call, out of a **24,000** total (`src/answering/gateway.py:31-37`).
- Measured reservation sizes: plan ≈ **5,643**, decision with no evidence ≈ **5,334**, decision with one evidence item ≈ **5,945**. Plan plus two sources × two steps ≈ **28,201**, which exceeds 24,000.
- Specialists catch the decision-hook failure and degrade. But `compose()` calls `gateway.structured` with no handler (`graph.py:140-143`), so `model_budget_exhausted` escapes the graph and the API returns **503**.
- **Fix:** reserve the writer's share up front, and fall back to the extractive writer when the model budget runs out.

### D2. Several uncoordinated deadlines — *Code reading*
- `RequestContext.deadline_at` is computed (`src/api/main.py:134`) but never enforced. The graph timeout, `CallBudget`, and `ModelGateway` each start their own 30-second clock after the session load.
- `/ask` maps `TimeoutError` to 504. The SSE path turns everything into a generic `answer_unavailable`.

### D3. A new MCP server process per branch per request — *Code reading*
- In stdio mode, every Jira or Slack branch starts `python -m src.integrations.mcp.server` (`src/integrations/mcp/client.py:58-65`). The readiness probe starts two more whenever its 5-second cache expires (`src/api/readiness.py:27-41`).
- **Answer:** "stdio is the strictest isolation for a demo. In production I'd use long-lived Streamable HTTP servers with pooled sessions. That transport is already supported (`MCP_TRANSPORT=http`)."

### D4. Possible leak of admission slots on early SSE disconnect — *Code reading, not reproduced*
- `prepare()` takes the slot. For `/api/chat` it is released only in the generator's `finally` (`src/api/main.py:185-189`). If the client disconnects before Starlette begins iterating the generator, that `finally` never runs. With a per-user limit of 2, two such leaks lock the user out until restart.
- **Fix:** release the slot in a `BackgroundTask` or a try/finally around `StreamingResponse` creation, or take the slot inside the generator.

### D5. All state is process-local — *Code reading*
- Sessions, admission counters, telemetry (a 500-event deque), and the readiness cache all live in memory. Restarting loses them, and replicas would not share them. This is stated in `src/api/admission.py:8` and `docs/architecture/gap-register.md` (G19).

### D6. No retry on transient model errors
- `AsyncOpenAI(max_retries=0)` (`gateway.py:41`). A 429 fails that step. This is deliberate under a deadline, but worth saying.

---

## E. Sessions and memory

### E1. Global session cap, which empty sessions also count against — *Code reading*
- Every request without a `session_id` creates a session, even if nothing is saved. The cap is **1,000 across all users**, not per user (`src/storage/sessions.py:32-38`). One user who sends many new-session requests can block every other user for the TTL (1 hour). The response is `404 session_unavailable`, which is misleading.
- **Fix:** a per-user cap, lazy creation (only on save), and a 429-style error.

### E2. Any change to a cited source deletes the whole conversation — *Code reading*
- `load_current_session` compares each cited version with the current one. On any mismatch, including a harmless new revision, `invalidate()` deletes the **entire session** (`src/orchestration/graph.py:220-234`, `src/storage/sessions.py:78-82`).
- **Answer:** "Correct for revocation, too aggressive for edits. I'd tombstone only the affected turns."

### E3. Answer saved but never delivered — *Code reading*
- If the client disconnects after `sessions.save` but before the SSE event is sent, memory holds an answer the user never saw. Minor.

---

## F. Ingestion and publication

### F1. Revisions that aren't numbers — *Code reading*
- Ordering protection (`new < old`) only applies to numeric revisions (`src/vectorstore/sqlite.py:102-107`). Web documents use a sha256 revision, so an older fetch can replace a newer one.
- Worse: once a document with a non-numeric revision is revoked, it **can never be republished**, because "A revoked revision requires verified newer source metadata" can't be satisfied.

### F2. File modification times as revisions
- Local files use `st_mtime_ns` as the revision. Restoring a file from a backup (older mtime) raises `PublicationConflict`. Touching a file without changing it forces re-chunking and re-embedding, because the revision is part of the digest.

### F3. No dedupe for uploads — *Reproduced*
- Uploading the same file twice → two documents with different UUIDs (`src/api/main.py:238`).

### F4. Parser sandbox on Windows — *Code reading*
- The parser subprocess sets `RLIMIT_CPU`, `RLIMIT_AS`, and `RLIMIT_FSIZE` only when `os.name != "nt"` (`src/ingestion/parser_worker.py:14-18`). The README's main platform is Windows, where only the 10-second wall-clock timeout applies.

### F5. Zip-bomb check trusts declared sizes
- The `.docx` check sums `info.file_size` from the central directory, which an attacker controls (`src/ingestion/extractors.py:122-125`). The real limit is `RLIMIT_AS` in the subprocess, which doesn't apply on Windows (see F4).

### F6. Pruning is conservative, sometimes too much so
- One unreadable file marks the scan incomplete, and then **nothing** in that scope is deleted (`src/connectors/filesystem.py:58-64`, `src/ingestion/pipeline.py:190-193`). That's safe but leaves stale documents. Say this is intentional: false deletion is worse than staleness.

### F7. Changing the embedding model prevents startup — *By design*
- The `index_configuration` table locks the embedding fingerprint and dimension (`sqlite.py:76-82`). Switching models raises an error at startup, so you must build a new index and cut over.

---

## G. Auth, trust, and deployment

### G1. The demo "local only" check behind a proxy
- `Authenticator` accepts demo tokens only from loopback IPs (`src/policy/auth.py:22-25`). Behind the bundled Nginx (which shares the network namespace), every request comes from 127.0.0.1. That's acceptable only because the proxy is published on the host's loopback interface.

### G2. An identity provider outage looks like a bad token
- If the JWKS fetch fails, the user gets `401 invalid_credentials` (`auth.py:38-41`). An IdP outage should be a 503.

### G3. The revocation recheck bypasses MCP
- `recheck_revocations` and `load_current_session` call `FixtureRepository.current()`, which reads the local JSON directly instead of going over MCP (`graph.py:207-218`, `224-230`). The screening step also checks records against that same local copy (`screening.py:98-103`).
- **Answer:** "It simulates an authoritative policy service that's separate from the tool path, so a tampered tool reply can't approve itself. In production that would be the provider's permissions API or a policy decision point."

### G4. Hardcoded key for signing pagination cursors
- `_CURSOR_KEY` is a constant in `src/integrations/mcp/repository.py:24`. That's fine for fixtures, but a real server would need a secret, rotated key.

### G5. Readiness with an empty index
- `/health/ready` returns 503 unless the documents index has chunks (`src/api/readiness.py:43-47`). A fresh deployment with `BOOTSTRAP_DEMO=false` is never "ready", so a load balancer would never send it traffic.

### G6. Host allowlist
- `TrustedHostMiddleware` allows only localhost names. Opening the app by LAN IP returns 400.

---

## Design questions — short answers

| Question | Answer |
|---|---|
| **Why MCP instead of calling the Jira/Slack APIs directly?** | A standard tool protocol with discoverable catalogs and a process boundary. Here it's hardened: the client pins expected tool schemas (`validate_catalog`), so a server that adds or changes a tool can't widen permissions. Arguments are strict Pydantic models. Raw replies never leave the adapter. |
| **Why LangGraph?** | Explicit nodes and conditional edges make the control flow auditable: plan → read → assess → (refine once) → write → validate → finalize. `recursion_limit=16` plus a single refinement round bounds the loops. |
| **How do you stop hallucinations?** | The writer may only emit verbatim excerpts. `validate_output` checks that each claim is a substring of its cited evidence, and that the evidence IDs exist. Citations are built from approved metadata, not from model text. |
| **How do you handle prompt injection?** | Layered. Screening at ingestion and at tool replies, read-only allowlisted tools, a code-owned budget and permissions, an extractive gate, and no draft streaming. Admit that the regex layer can be bypassed (A2) and that it produces false positives (A1). |
| **Why recheck revocation twice?** | A source can be revoked while the answer is being written. The first check prevents saving stale evidence to memory. The second catches a revocation that lands between saving and release, and also invalidates the memory. |
| **Why doesn't memory cache answers?** | A cached answer can outlive its permissions. Follow-ups reuse only the previous *question* to resolve references, and always re-read sources. |
| **Why SQLite?** | One transaction covers the content, the vectors, and the active-version pointer, so publication is atomic and revocation is immediate. FAISS and OpenSearch are optional accelerators, and SQLite stays the authority. |
| **Why combine BM25 and vectors (RRF, k=60)?** | Identifiers like `PAY-123` need exact matching, and prose benefits from vectors. RRF fuses the two rankings without having to calibrate their scores against each other. |
| **Why a shared tool budget?** | Parallel branches and the refinement round draw from one atomic ledger (`CallBudget.reserve` under a lock). A planner that emits many tasks can't multiply cost. Failed and timed-out calls still count. |
| **Why parse uploads in a subprocess?** | Parser bugs, hangs, and memory blowups in pypdf or python-docx are isolated with a deadline and rlimits (not on Windows, see F4). This is fault isolation, not a full sandbox. |
| **Why screen before embedding?** | Embeddings and indexes are hard to purge. A secret that gets embedded has already leaked into derived data. |
| **What would you change for production?** | Shared state (Redis or Postgres) for sessions, admission, and budgets. Long-lived MCP over HTTP with real OAuth. A real policy service for revocation. An ANN index. Per-claim gating instead of all-or-nothing. Fixes for A1, A4, B1, C7, D1, E1, and F1. |

---

## Rebuilding the reproductions

The script used for the *Reproduced* items exercised `sanitize_text`, `authorize_tool_call`, `merge_evidence`, `validate_output`, `resolve_followup`, `deterministic_plan`, and the `/api/ask`, `/api/documents/upload`, and `/api/sessions` endpoints against `create_app(Settings(app_env="test", model_mode="demo", mcp_transport="stdio"))`, the same setup as `tests/test_workflow.py`. To turn any item into a regression test, copy the client fixture from that file and assert the behavior quoted above.
