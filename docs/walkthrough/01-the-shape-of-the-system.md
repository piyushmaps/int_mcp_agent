# 1 — The shape of the system

## The problem being solved

An engineer asks: *"How do I create a new capability, and who owns Payments?"*

The answer to that one sentence lives in four different places: the official
guide is in DevPortal, the gotchas are in an old Slack thread, the open work is
in Jira, and the ownership fact is in the city map. No single search box covers
all four, and none of them can write you a combined answer.

This project is that search box plus the writer.

## It is two programs, not one

This is the first thing to get straight, because almost every design decision
follows from it.

**Program A — ingestion. Runs offline, occasionally.**

```
connectors → Documents → hash → chunks → embeddings → vector index
```

You run it with `python -m src.ingestion.pipeline`. It may take minutes. It
touches the OpenAI embedding API thousands of times. Nobody is waiting on it.

**Program B — serving. Runs online, per question, in about a second.**

```
question → route → search index → synthesize answer → JSON response
```

You run it with `uvicorn src.api.main:app`. A user *is* waiting on it.

The vector index is the only thing they share. Ingestion writes it; serving
reads it. That is the seam of the whole system:

```
  ┌─────────────┐   writes    ┌──────────────┐   reads    ┌──────────────┐
  │  ingestion  │ ──────────▶ │ vector index │ ◀───────── │  the agent   │
  │ (slow, batch)│            │  (FAISS or   │            │ (fast, HTTP) │
  └─────────────┘             │  OpenSearch) │            └──────────────┘
                              └──────────────┘
```

Because they are separate, "the search is slow" and "ingestion is slow" are
different problems with different fixes, and you can rebuild the index without
touching the API.

## The three ideas you need before reading code

**1. An embedding is meaning as numbers.** You send a piece of text to an
embedding model and get back a fixed-length list of floats — 1536 of them for
`text-embedding-3-small`. Texts that mean similar things come back as lists that
point in a similar direction. That is the whole trick. "Who owns Payments?" and
"Payments capability owner team" land near each other even though they share
only one word.

**2. A vector index finds nearest neighbours.** Give it your question's
embedding and it returns the *k* stored chunks whose embeddings point most
nearly the same way. It does not understand anything; it does geometry, fast.

**3. RAG = retrieve, then generate.** The LLM is never asked "what do you know
about Payments?" — it would guess. It is asked "here are six pieces of text we
found; answer using only these, and cite them." Retrieval supplies the facts;
the LLM only supplies the writing. That is why answers can carry citations and
why the system can say "I don't know."

**Agentic RAG** adds one step before retrieval: instead of blindly searching
everything, an LLM first decides *which sources* to search and *whether the
question should be split into parts*. Chapter 9 covers that.

## Run it once before reading further

```bash
.venv/Scripts/python.exe -m src.ingestion.pipeline    # builds data/index/faiss
.venv/Scripts/python.exe -m uvicorn src.api.main:app --port 8000
```

Then in another terminal:

```bash
curl -X POST http://localhost:8000/ask -H "Content-Type: application/json" \
  -d "{\"question\": \"Who owns the Payments capability?\"}"
```

Look at the JSON that comes back. It has `answer`, `citations`,
`selected_sources`, `routing_rationale`, `sub_queries`. Every one of those
fields is produced by a specific function you are about to read.

Now ask it something off-topic — `"What is the capital of France?"` — and note
that it refuses with zero citations. That refusal is not the LLM being modest;
it is a threshold in [`retriever.py`](../../src/retrieval/retriever.py). Chapter 8.

## The map of the code

Read this table as "where would I go if…":

| Folder | Answers the question |
|---|---|
| [`src/models/`](../../src/models/) | What shape is knowledge in this system? (ch. 2) |
| [`src/connectors/`](../../src/connectors/) | How does each source become that shape? (ch. 3–4) |
| [`src/ingestion/`](../../src/ingestion/) | How does text become a searchable index? (ch. 4–5, 7) |
| [`src/vectorstore/`](../../src/vectorstore/) | Where do the vectors actually live? (ch. 6) |
| [`src/retrieval/`](../../src/retrieval/) | Given a question, which chunks come back? (ch. 8) |
| [`src/agent/`](../../src/agent/) | How is a question turned into an answer? (ch. 9) |
| [`src/api/`](../../src/api/) | How is that exposed over HTTP? (ch. 9) |
| [`src/eval/`](../../src/eval/) | Is it any good? (ch. 10) |
| [`src/observability/`](../../src/observability/) | What did it actually do on that request? (ch. 10) |
| [`src/config.py`](../../src/config.py) | Every tunable number in the system |

`src/config.py` is worth skimming right now. It is one class, `Settings`, read
from `.env` by pydantic-settings, and it is the only place where a number like
"chunk size 800" or "similarity cutoff 0.28" is written down. When a later
chapter says "the configured batch size", it means a field on that class.

## Say this out loud

> "It's two programs sharing one index. Ingestion is a slow offline job that
> turns four sources into embedded chunks. Serving is a fast HTTP path that
> routes a question to the right sources, retrieves chunks, and has an LLM write
> a cited answer from only those chunks."

## Prove it to yourself

1. Run ingestion twice in a row. The second run reports almost everything as
   "unchanged (skipped)" and costs no API calls. You will learn why in ch. 7.
2. Delete `data/index/faiss/` and hit `/health`. It reports `degraded`, and
   `/ask` returns 503 instead of crashing. Find the code that does that in
   [`src/api/main.py`](../../src/api/main.py) — about 15 lines.
