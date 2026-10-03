# 11 — What you got right, and what is still missing

This chapter is for the conversation where someone asks "so what did *you*
build?" Every claim below is defensible from the code, and every gap below is one
you should raise yourself rather than have found for you. Naming your own
weaknesses is what makes the strengths believable.

## 11.1 The ten decisions worth defending

| # | Decision | The one-line defence |
|---|---|---|
| 1 | Normalize every source to one `Document` at the boundary (ch. 2) | Chunking, embedding, indexing, retrieval and citation are written once instead of once per source. A fifth source is five small edits and nothing downstream changes. |
| 2 | Connectors stream (`yield`) rather than return lists (ch. 3) | A source can be far bigger than memory. Peak memory is set by batch size, not corpus size. |
| 3 | Profile large sheets instead of indexing every row (ch. 4) | 1,149,072 documents became 4,577; roughly $5 and hours became $0.13 and minutes, with no loss of anything a question could reach. |
| 4 | Source-aware chunking (ch. 5) | Splitting a five-message Slack thread destroys the Q&A context that makes it retrievable at all, so short conversational documents are kept whole. |
| 5 | One `VectorStore` protocol, two backends (ch. 6) | FAISS needs no server so the project runs on a laptop; OpenSearch gives filtering, per-document deletes and hybrid search at scale. One config value switches them and nothing else knows. |
| 6 | Score means cosine similarity in **every** backend (ch. 6) | Every relevance decision downstream depends on it. It is enforced in each backend, verified against a live cluster by a test, and one real bug (BM25 scores leaking past the cutoff) came from breaking it. |
| 7 | Backends own their workarounds; the retriever owns policy (ch. 6, 8) | FAISS's escalating over-fetch window lives inside FAISS. Callers never inherit one backend's limitation, and OpenSearch does not pay for it. |
| 8 | A measured relevance cutoff (ch. 8) | A vector index always returns k neighbours however distant. 0.28 sits in the empty gap between on-topic scores of 0.69–0.85 and off-topic scores below 0.09, so the system can honestly refuse — before spending an LLM call. |
| 9 | Incremental, resumable ingestion via a content-hash manifest (ch. 7) | Unchanged documents are never re-embedded, a run killed at 70% resumes at 70%, and documents deleted at source are prunable. Resumability wasn't built — it falls out of idempotency. |
| 10 | Evaluation with a plain-RAG baseline (ch. 10) | The agent layer's cost is measured against a control, so "the agent helps" is a number, not a belief. `--fail-under` makes it a CI gate. |

## 11.2 The numbers to remember

- **1,149,072 → 4,577 documents** from sheet profiling; ~$5 → ~$0.13.
- **0.28** relevance cutoff; on-topic 0.69–0.85, off-topic ≤0.09.
- **800 / 120** chunk size and overlap; **256** chunks per embedding batch;
  checkpoint every **20** batches.
- **200 characters** average cell length is the prose-column heuristic.
- **59** tests, running in ~12 seconds with no network and no API key.

## 11.3 Questions you should be ready for

**"Why not just put everything in one prompt?"** Context limits and cost, but
mostly precision — an LLM given 4,000 documents finds the relevant one less
reliably than a retriever does, and you pay for every token on every request.

**"Why one index instead of one per source?"** Scores are comparable inside one
embedding space, so a cross-source question is one ranked search instead of four
searches and an unprincipled merge. Routing is a query-time *filter*, not a
physical partition. You would split a source out when it dominates in size,
updates on a very different cadence, or needs its own security boundary.

**"Why agentic instead of plain RAG?"** Routing spends the same k retrieval slots
on the sources that can actually answer, and decomposition handles multi-part
questions that a single embedding blurs together. The cost is one extra LLM call
and a new failure mode (routing to the wrong source), which is exactly why
routing is scored separately and compared against a baseline.

**"How do you know it works?"** Golden set, two metrics, a plain-RAG control, and
a threshold that fails the build. Plus per-request traces with retrieval scores,
which separate bad retrieval from bad generation.

**"What happens when the LLM returns garbage?"** The router's JSON is parsed
strictly, then loosely, then defaulted; source names are validated against the
enum; empty results fall back to `devportal + city_map`. The system degrades to
plain RAG rather than failing.

**"What breaks first at scale?"** FAISS — everything is resident and every query
scans every vector, plus the entire chunk payload is one JSON file. That is the
switch to OpenSearch, which is already implemented and tested. After that, the
embedding API is the ingestion bottleneck; batching and the skip-unchanged path
are the mitigations already in place.

## 11.4 The honest gaps

Raise these yourself.

1. **The connectors read sample files, not live APIs.** `.env.example` has slots
   for Jira and Slack credentials that nothing consumes yet. For Jira, the change
   is the two lines that load the JSON (ch. 3.2); the normalization below them
   already works. Real APIs add pagination, rate limits, incremental sync
   (`updated_at > last_run`) and auth — none of which exist today.
2. **The golden set is five questions.** Enough to catch a regression, not enough
   to trust a small improvement. Twenty to fifty would make the numbers mean
   something.
3. **No test covers the API layer.** Fifty-nine tests and none of them touch
   `src/api/main.py`. FastAPI's `TestClient` would make this a short file, and
   the lifespan/503 behaviour is worth pinning down.
4. **No reranker.** Retrieval returns the top k by embedding similarity, with no
   second-stage model reordering them. A cross-encoder reranker is the usual next
   quality win, and there is no hook for one yet.
5. **No conversational memory.** Every `/ask` is independent. "What about
   Payments?" after a previous question cannot work, because nothing carries
   over.
6. **`HOW_IT_WORKS.md` has drifted.** It still describes L2 distances and a
   `relevance_threshold` of 1.2 from before the cosine refactor, and points at a
   `tests/test_retrieval.py` that is now `tests/test_retriever.py`. The design
   narrative is still excellent; the numbers in it are stale. Trust the code.
7. **The dedupe key is approximate** — `(url, first 120 chars)` rather than a
   chunk id (ch. 9.4). Collisions are rare and cheap, but it is a heuristic.
8. **No authentication, rate limiting, or per-user access control on `/ask`.**
   Fine for a demo; the first thing to add if this ever fronted real internal
   documents, since retrieval would happily cite a document the asker should not
   see.
9. **Nothing schedules ingestion.** It is a manual command. The AWS scaffolding
   in `infra/` sketches S3-triggered Lambda re-ingestion, but it is a stub.

## 11.5 Where to go next

Roughly in order of value per hour spent:

1. Expand the golden set to ~25 questions, including several the corpus cannot
   answer (refusals are worth testing too).
2. Add `tests/test_api.py` with FastAPI's `TestClient`.
3. Make one connector real — Jira is the easiest, since the credentials and the
   normalization already exist.
4. Add a reranking step between retrieval and synthesis, and measure the lift
   with the harness you already have. You have the control group; use it.
5. Add conversational memory: carry the last turn's question and answer into the
   routing prompt.
6. Refresh `HOW_IT_WORKS.md` so the numbers match the code.

## 11.6 The paragraph to say when someone asks what you built

> "It's an agentic RAG assistant over four internal knowledge sources. Everything
> normalizes to one document schema at the connector boundary, so the rest of the
> system is written once — and connectors stream, so a source bigger than memory
> is fine. Ingestion is incremental and resumable: documents are fingerprinted in
> a SQLite manifest, so an unchanged corpus costs nothing to re-run and a killed
> job resumes where it stopped. Large spreadsheets get profiled rather than
> enumerated, which took the corpus from 1.1 million documents to about 4,500
> with no loss of answerable content. There are two vector backends behind one
> protocol — FAISS locally, OpenSearch with filtering and hybrid keyword+vector
> search at scale — and they guarantee that a score always means cosine
> similarity, which is what makes a measured relevance cutoff possible, which is
> what lets the system say 'I don't know' instead of citing six chunks that
> support nothing. At query time an LLM routes to the right sources and splits
> multi-part questions, retrieval runs concurrently, and synthesis answers only
> from the evidence with citations filtered to what it actually used. It's scored
> against a golden set on routing and grounding separately, with a plain-RAG
> baseline as the control, and every request can be traced with retrieval scores."

Read that once a day until it is yours. Then read the code again — it will look
different now.
