# 12 — Telling this project as a story

An interviewer is not asking for a tour of your folders. They are asking three
questions, whether or not they say them out loud:

1. Did you understand the problem, or just build what you were told?
2. Did you make decisions, and can you explain the trade-offs?
3. Do you know what's weak in your own work?

A folder-by-folder walkthrough answers none of those. A story answers all three.

## The shape

```
1. The problem            30 seconds   why this exists at all
2. The shape of it        45 seconds   what you built, in one picture
3. Two or three moments   3-4 minutes  ← this is the whole interview
4. How you know it works  45 seconds   the eval harness
5. What you'd do next     30 seconds   the gaps, volunteered
```

Step 3 is the part that gets you hired. A "moment" is: something surprised me →
I looked → here is what was actually happening → here is what I changed → here is
the measurement. Everything else is scaffolding around those.

---

## 1. The problem (start here, always)

> "An engineer asks: *how do I create a new capability, and who owns Payments?*
> The answer to that one sentence is split across four systems — the official
> guide is in DevPortal, the gotchas are in an old Slack thread, the open work is
> in Jira, and the ownership fact is in a service graph we call the city map. No
> single search box covers all four, and none of them can write you a combined
> answer. So I built the thing that can."

Never open with the tech stack. "I used LangGraph, FAISS and OpenSearch" tells
them nothing about whether you should have.

## 2. The shape

> "It's really two programs sharing one index. Ingestion is a slow offline job:
> connectors read the four sources, everything normalizes into one document
> shape, gets chunked, embedded, and written to a vector index. Serving is a fast
> path: a question comes in, an LLM decides which sources are worth searching,
> retrieval runs across them in parallel, and the answer is written using only
> the retrieved text, with citations back to the source.
>
> The one design decision everything else hangs off is that normalization step.
> Four sources with completely different shapes all become one `Document` at the
> connector boundary — so chunking, embedding, indexing, retrieval and citation
> are each written once instead of once per source. Adding a fifth source is a
> new connector and four small edits. Nothing downstream changes."

That's enough. Stop and let them steer, or move into your moments.

---

## 3. The moments

Have three ready. Lead with the spreadsheet one — it's the strongest, because it
has a surprise, a diagnosis, and a number.

### Moment 1 — "index everything" is wrong

> "Everything worked fine on sample data. Then I dropped in a folder of real
> exports — a 900,000-row crime table, a 60,000-row insurance table, three
> calculus textbooks — and ingestion produced **1,149,072 documents**. Several GB
> of vectors, hours of embedding, about $5.
>
> So I looked at what was actually being embedded, and a typical row was
> `4.5, 2.3, 1.3, 0.3, Setosa`. Rows like that all embed to nearly the same
> point — semantic search over a million of them returns noise. And nobody is
> ever going to ask a question whose answer is row 448,201.
>
> So I stopped treating a sheet by its file extension and started treating it by
> what it *is*. A small reference table — under 500 rows, like an ownership
> roster — still gets one document per row, because 'who owns Payments' should
> match exactly one row. A large data table gets **one profile document**
> instead: schema, row count, per-column statistics, a few sample rows. That
> answers the question people actually ask about a big table, which is 'what data
> do we have about crime?'. And a large table that carries prose — a column of
> news articles or ticket bodies — gets the profile *plus* its rows up to a cap,
> because those genuinely are worth retrieving.
>
> Detecting prose is a heuristic: if a column's cells average over 200
> characters, it's prose, not a category or a number.
>
> That took the corpus from **1,149,072 documents to 4,577**, and from roughly $5
> and several hours to about $0.13 and minutes — without losing anything a
> question could actually reach."

**Why this lands:** it's a real surprise, you diagnosed it by *looking at the
data* rather than guessing, the fix is a judgement about meaning rather than a
library swap, and it ends in a number.

**Follow-up you should expect — "how does that heuristic fail?"** Answer it
honestly: a column of 150-character descriptions gets missed, and a column of
long URLs or base64 blobs gets falsely called prose. Both are acceptable — the
cost is a few thousand mediocre chunks, not a million.

### Moment 2 — teaching it to say "I don't know"

> "I asked it something completely off-topic — 'what is langchain?' — and it came
> back with a confident answer and six citations.
>
> The cause isn't the LLM. A vector index has no concept of 'no match'. It always
> returns the k nearest neighbours, however distant they are. So it returned the
> six *least dissimilar* chunks in the corpus, which were about nothing relevant,
> and the LLM had been told to answer from the evidence — so it did.
>
> The fix is a relevance cutoff, but I needed an actual number rather than a
> guess. So I measured: on-topic questions score between 0.69 and 0.85 cosine
> similarity, off-topic ones score 0.09 and below. That's a wide empty gap, so I
> put the cutoff at 0.28 — far from both clusters, so small errors don't change
> the outcome. When everything falls below it, retrieval returns nothing and
> synthesis short-circuits to a refusal **before** spending an LLM call.
>
> A RAG system that always answers is a RAG system that will confidently make
> things up. Honest refusal is a feature, not a limitation."

**Why this lands:** it shows you understand what a vector index actually does,
and that you chose a threshold by measurement instead of vibes. That contrast —
"this number was measured, that one is a sensible default" — is exactly the
judgement interviewers are probing for.

### Moment 3 — the invariant I broke myself

> "I have one rule the whole system depends on: a search score always means
> cosine similarity, higher is better, in every backend. FAISS natively returns a
> distance and OpenSearch returns its own scaled score, so both get converted
> inside their own module — callers never branch on which store is behind them.
>
> Then I added hybrid search on OpenSearch: keyword search alongside vector
> search, because pure semantic search misses exact tokens like ticket keys —
> every ticket key looks alike to an embedding model. And off-corpus questions
> started getting answered again, but only on that backend.
>
> The reason was that keyword hits were carrying their **BM25** scores. BM25
> scores are unbounded — they can be 12 or 40 — and my relevance cutoff compares
> against 0.28 assuming cosine. So a chunk that merely shared one common word
> with the question sailed straight past the cutoff and got cited.
>
> The fix keeps BM25 for *ranking* — it still decides the order of that list —
> but recomputes the score as cosine against the stored embedding. So the score
> means the same thing whichever ranking a chunk arrived on. And I added a test
> that verifies the score conversion against a live cluster by indexing vectors
> at known angles, rather than trusting the documentation."

**Why this lands:** you designed an invariant, you violated it yourself in a
non-obvious place, you caught it, and you wrote a test so it can't come back.
That's a more convincing story than any bug you found in someone else's code.

### A fourth, if design taste comes up

> "The FAISS backend has a limitation: no metadata filtering, so restricting a
> search to two sources has to happen *after* the search. The naive version —
> fetch 3× and filter — fails quietly when the routed sources are a small slice
> of the corpus: their chunks fall outside the global top-N and you get nothing
> back, so narrowing the route makes retrieval *worse*. So the window escalates,
> ×3 each round, until it has enough hits or the index is exhausted.
>
> The part I'd point at, though, is where that code lives. It used to be in the
> retriever, which meant every caller inherited one backend's limitation —
> including OpenSearch, which filters natively and doesn't need it. I moved it
> into the FAISS module. The retriever now holds only policy: embed the query,
> ask for the routed sources, drop anything below the cutoff. Storage mechanics
> belong to storage."

---

## 4. How you know it works

> "There's a golden set of questions with known-correct properties, and it scores
> two things **separately**: routing — did the router pick the sources the
> question needs — and grounding — did the answer actually state the facts it
> must mention. Keeping them apart is what makes a failure diagnosable: if
> grounding drops but routing is fine, the problem is retrieval or synthesis; if
> routing is bad, the right source was never searched.
>
> Routing is scored as recall and precision, deliberately not exact match. A
> router that picks two sources when one would do still surfaces every fact —
> exact match would score that a failure and report 30% accuracy for a system
> answering everything correctly. A metric that punishes harmless behaviour will
> push you to break a working system.
>
> And there's a plain-RAG baseline as a control — same corpus, same retriever,
> same synthesis prompt, with routing removed. So the agent layer's value is a
> measured number rather than a belief. It can run in CI with a threshold, so a
> prompt change that quietly breaks answers fails the build."

## 5. What you'd do next

Volunteer this. Don't wait to be asked.

> "The honest gaps: the connectors read sample files rather than live APIs — the
> normalization below that is already right, but real APIs add pagination, rate
> limits and incremental sync. The golden set is only five questions, which is
> enough to catch a regression and not enough to tune on, so expanding it is the
> first thing I'd do. There's no reranker yet, and no conversational memory. And
> there's no access control — retrieval doesn't know who's asking, so if you
> indexed a private channel it would happily cite it to anyone. That's the first
> thing I'd fix before this went anywhere near real internal documents."

---

## Delivery rules

1. **Problem first, stack last.** Name a technology only when explaining why you
   chose it over the alternative.
2. **Numbers are what people remember.** 1,149,072 → 4,577. On-topic 0.69–0.85,
   off-topic below 0.09. Have them ready.
3. **Volunteer one weakness before they find it.** It converts every other claim
   from a sales pitch into a report.
4. **Say "I don't know" cleanly, then say how you'd find out.** Exactly what you
   taught the system to do.
5. **Never claim what isn't built.** If asked about the Kafka/Lambda streaming
   path in `HOW_IT_WORKS.md`, say: "that's the design for keeping the index
   fresh; what runs today is a batch command over sample files." Precision about
   design-versus-implemented is a *credibility gain*, not a loss — and a claim
   that collapses under one follow-up costs you the whole interview.
6. **Don't say "agentic" without immediately defining it.** "An LLM decides which
   sources to search before retrieving, instead of blindly searching everything."

## Questions you will get

- *Why not put everything in one prompt?* → Cost and context limits, but mostly
  precision: an LLM given 4,000 documents finds the relevant one less reliably
  than a retriever does, and you pay per token per request.
- *Why one index instead of one per source?* → Scores are comparable inside one
  embedding space, so a cross-source question is one ranked search instead of
  four and an unprincipled merge. Routing is a query-time filter, not a physical
  partition. You'd split a source out when it dominates in size, updates on a
  very different cadence, or needs its own security boundary.
- *Why agentic instead of plain RAG?* → Routing spends the same k retrieval slots
  on sources that can actually answer, and decomposition handles multi-part
  questions. The cost is an extra LLM call and a new failure mode — routing to
  the wrong source — which is exactly why routing is scored separately against a
  baseline.
- *What breaks first at scale?* → FAISS. Everything is resident and every query
  scans every vector, and the chunk payload is one JSON file. That's the switch
  to OpenSearch, which is already built and tested. After that the embedding API
  is the ingestion bottleneck, which batching and skip-unchanged already
  mitigate.
- *How do you handle a changed or deleted document?* → Every document is
  fingerprinted in a SQLite manifest, so unchanged ones are never re-embedded.
  Chunk ids are deterministic, so a re-ingest overwrites. A shrunk document would
  leave orphan chunks, so an update deletes the document's chunks first. And
  documents that vanish from a source are detected by comparing what this run saw
  against what the manifest remembers — which is only safe after a complete pass,
  so a partial run must not prune.
- *What would you do differently?* → Measure chunk size instead of taking the
  800/120 default, and design the access-control model before indexing anything
  real rather than after.

## The 60-second version

For "tell me about a project you've worked on":

> "It's an internal knowledge assistant over four sources — Jira, Slack, our
> developer portal, and a service-ownership graph. The problem is that one
> question often spans all four, and no single search box covers them.
>
> Everything normalizes into one document shape at the connector boundary, gets
> chunked and embedded into a vector index ahead of time. At query time an LLM
> routes the question to the right sources, retrieval runs in parallel, and the
> answer is written only from the retrieved text with citations back to source.
>
> The two things I'd point at: it knows when to refuse — I measured the
> similarity gap between on-topic and off-topic questions and put a cutoff in it,
> so an off-corpus question returns nothing rather than six citations that
> support nothing. And ingesting real messy data taught me not to index
> everything: profiling large spreadsheets instead of embedding every row took
> the corpus from 1.1 million documents to about 4,500, with no loss of anything
> answerable."

Then stop. Let them pick which thread to pull.
