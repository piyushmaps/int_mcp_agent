# 9 — The agent: route, retrieve, synthesize

**Files:** [`state.py`](../../src/agent/state.py),
[`nodes.py`](../../src/agent/nodes.py) (the three steps),
[`graph.py`](../../src/agent/graph.py) (the wiring),
[`api/main.py`](../../src/api/main.py) (HTTP).

This is the "agentic" in agentic RAG. Plain RAG is: embed the question, search
everything, generate. This adds a **thinking step before retrieval** — an LLM
decides which sources to search and whether the question should be split up.

## 9.1 Why routing earns its keep

Ask *"Who owns Payments?"* Plain RAG searches all four sources and its top 6
results include a DevPortal how-to and two Jira tickets that mention Payments.
Those are noise: they crowd out the ownership record and give the LLM tempting,
irrelevant material.

Routing first says "this is an ownership question → city_map, maybe slack", and
the same six slots now hold six ownership-related chunks. **Same k, better
evidence.** Precision at the source level, before you spend it on ranking.

The cost is real: one extra LLM call (about 300ms and a fraction of a cent) and
a new failure mode — routing to the wrong source means the right answer is not
even searched. That is why chapter 10's harness scores routing separately, and
why a baseline is measured against it. You should be able to state both sides.

## 9.2 The state

```python
class AgentState(TypedDict, total=False):
    question: str
    selected_sources: list[str]
    sub_queries: list[str]
    retrieved: list[RetrievedChunk]
    answer: str
    citations: list[dict[str, Any]]
    routing_rationale: str
```

A `TypedDict` is a plain dictionary with declared key types — no class, no
methods, just a shape a type checker can verify. `total=False` means every key is
optional, which is required here because the state is built up progressively:
after routing only three keys exist, after retrieval four, after synthesis all
seven.

The whole graph is "a dict flows through functions, each adding keys". That
simplicity is why the nodes are so easy to test individually — chapter 10's
baseline calls `synthesize_answer` directly with a hand-built dict.

## 9.3 Node 1 — route

[`route_sources`](../../src/agent/nodes.py#L27-L74). The prompt is worth reading
closely, because prompt design *is* engineering here:

```
Available sources:
- jira: tickets, decisions, open work
- slack: tribal Q&A, how-tos from engineers
- devportal: official documentation and checklists
- city_map: ownership, dependencies, capability graph
...
Respond with ONLY valid JSON (no markdown): { "sources": [...], "sub_queries": [...], "rationale": "..." }

Rules:
- Pick 1–3 sources most likely to answer.
- Ownership / who-owns / dependencies → include city_map.
- How-to / checklist / process → include devportal; often also slack.
- Tickets / status / open work → include jira.
- If unsure, include devportal + city_map.
```

Four things being done deliberately:

1. **Each source is described by what it contains**, not just named. The model
   cannot route to `city_map` correctly unless it knows what a city map is. (This
   is why "add your source to the prompt" is step 5 of chapter 3's checklist.)
2. **Structured output is demanded** — JSON, explicitly "no markdown", with an
   example object. You are turning free text into a data structure.
3. **Explicit rules encode domain knowledge** the model cannot infer. These are
   the routing decisions *you* know are right.
4. **A default for uncertainty.** "If unsure, include devportal + city_map."
   Without it, an ambiguous question gets an arbitrary guess.

Then the defensive handling, which matters more than the prompt:

```python
raw = _llm().invoke(prompt).content
parsed = _parse_json(raw if isinstance(raw, str) else str(raw))

sources = [s for s in parsed.get("sources", []) if s in VALID_SOURCES]
if not sources:
    sources = ["devportal", "city_map"]

sub_queries = parsed.get("sub_queries") or [question]
```

**Never trust an LLM's output shape.** Three layers of defence:

```python
def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}
```

- Try strict JSON.
- If that fails, pull the first `{...}` out of the text — this is the "here is
  your JSON: ```json {...}```" case, which happens constantly.
- If that fails, return `{}` and let the caller's defaults take over.

Then the *values* are validated: sources not in `VALID_SOURCES` are dropped
(hallucinated source names cannot reach the store), an empty result falls back to
a sensible default, and missing sub-queries fall back to the original question.

The result: **the router can fail completely and the system still answers.** It
degrades to something close to plain RAG rather than crashing. That is the right
posture for anything built on a model's output.

`temperature=0` in `_llm()` asks for the most likely completion each time, which
makes routing about as reproducible as an LLM gets — important for the eval
harness to mean anything run to run.

## 9.4 Node 2 — retrieve

[`retrieve_evidence`](../../src/agent/nodes.py#L77-L106).

```python
with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4) or 1) as pool:
    per_query = list(pool.map(lambda q: retriever.retrieve(q, sources=sources), sub_queries))
```

Sub-queries are independent searches, and each one is dominated by waiting on the
network (embed the query, query the store). So they run **concurrently**.

Why threads rather than processes, given Python's GIL? Because the GIL is
released during I/O waits. Threads are the right tool for I/O-bound work; the GIL
only blocks CPU-bound parallelism. Three sub-queries take about as long as one.

`min(len(sub_queries), 4)` caps the pool — no point creating 20 threads for 20
sub-queries and hammering the embedding API. The `or 1` guards the empty case,
since `ThreadPoolExecutor(max_workers=0)` raises.

```python
seen: set[tuple[str, str]] = set()
retrieved = []
for chunks in per_query:
    for chunk in chunks:
        key = (chunk.url, chunk.content[:120])
        if key in seen:
            continue
        seen.add(key)
        retrieved.append(chunk)
```

Two sub-queries often retrieve the same chunk; sending it to the LLM twice wastes
context and skews the citation list. The dedupe key is `(url, first 120 chars)` —
not the chunk id, because a chunk id is not present on `RetrievedChunk` (chapter
8's boundary type deliberately dropped it). It is a heuristic; two different
chunks from the same URL sharing an opening 120 characters would collide. Rare
and low-cost, but you should know it is approximate rather than claim it is exact.

The comment about ordering is the subtle part:

> Dedupe in sub-query order so output stays stable regardless of completion order.

`pool.map` returns results in **input order**, not completion order, and the
dedupe walks that ordered list. So the same question always produces the same
evidence in the same order, even though the searches raced. Non-determinism here
would make debugging and evaluation miserable.

The tracing calls are covered in chapter 10; note only the comment explaining
that recording happens *after* the pool joins, because tracing context does not
cross thread boundaries.

## 9.5 Node 3 — synthesize

[`synthesize_answer`](../../src/agent/nodes.py#L109-L172).

**The short circuit comes first:**

```python
chunks = state.get("retrieved") or []
if not chunks:
    return {**state, "answer": "I couldn't find relevant evidence in Jira, Slack, "
            "DevPortal, or the city map. Try rephrasing or asking about capability "
            "creation, ownership, or open tickets.", "citations": []}
```

No evidence → no LLM call. Chapter 8's cutoff created this situation; this is
where it pays. The refusal also *tells the user what the system does know*, which
turns a dead end into a hint.

**Evidence is numbered:**

```python
for i, chunk in enumerate(chunks, start=1):
    evidence_blocks.append(f"[{i}] source={chunk.source} title={chunk.title}\nurl={chunk.url}\n{chunk.content}")
    citations.append({"index": i, "source": ..., "title": ..., "url": ...})
```

The same index `i` labels the text the LLM sees and the citation object the API
returns. That shared numbering is what makes `[2]` in the answer resolvable to a
real URL by the client.

**The prompt:**

```
Answer using ONLY the evidence below. Cite sources as [1], [2], etc.
If evidence conflicts, prefer DevPortal over Slack, and city map for ownership facts.
Be concise and actionable.
```

"ONLY the evidence" is the anti-hallucination instruction; the numbered citation
format is what the next block parses. And the **conflict rule** is genuine domain
knowledge: official docs outrank a Slack opinion, and the city map is the system
of record for ownership. Real corpora contradict themselves, and without a
precedence rule the model picks arbitrarily.

**Citation filtering — a small, thoughtful touch:**

```python
cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer_text)}
if cited:
    citations = [c for c in citations if c["index"] in cited]
```

Six chunks were retrieved, but the answer may only use two. Returning all six
citations implies six sources supported the answer, which is false. So the
returned list is filtered to the markers actually present in the text — while
keeping the **original indices**, so `[4]` in the prose still resolves to the
citation labelled 4.

And `if cited:` — if the model cited nothing at all, show everything rather than
nothing. A degraded answer beats an answer with no provenance at all.

## 9.6 The graph

[`graph.py`](../../src/agent/graph.py):

```python
retrieve_node = partial(retrieve_evidence, retriever=retriever)

graph = StateGraph(AgentState)
graph.add_node("route", route_sources)
graph.add_node("retrieve", retrieve_node)
graph.add_node("synthesize", synthesize_answer)

graph.add_edge(START, "route")
graph.add_edge("route", "retrieve")
graph.add_edge("retrieve", "synthesize")
graph.add_edge("synthesize", END)

return graph.compile()
```

`functools.partial(f, retriever=r)` pre-fills an argument, producing a
single-argument function — which is what LangGraph requires of a node
(`state -> state`). That is how the retriever gets injected without a global.

Right now this is a straight line, and a reasonable question is "why use a graph
framework for A→B→C?" The honest answer is: for what exists today you would not
need one — the value is what it makes cheap next. Conditional edges (retry
retrieval with different sources if nothing was found), loops (reflect on the
answer, search again), branches, checkpointing and streaming are all supported by
the framework and would each be an invasive rewrite in hand-rolled code. Say that
plainly rather than overselling the current graph.

```python
result = agent.invoke({"question": question},
                      config={"callbacks": langfuse_callbacks(), "run_name": "ask"})
```

One entry point, `run_agent`, returning a plain dict of the six fields the API
exposes. Callbacks are chapter 10.

## 9.7 The API

[`api/main.py`](../../src/api/main.py).

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    global _retriever, _chunk_count
    ...
    try:
        _retriever = SourceRetriever()
        _chunk_count = _retriever.store.count()
        if _chunk_count == 0:
            logger.warning("index is empty — run: python -m src.ingestion.pipeline")
    except Exception as exc:
        logger.warning("vector store unavailable (%s) — /ask will fail until it is", exc)
        _retriever = None
    yield
```

`lifespan` runs once at startup (everything before `yield`) and once at shutdown.
The retriever — which loads the FAISS index from disk, or opens an OpenSearch
connection — is built **once** here, not per request. Doing it per request would
re-read the index on every question.

The try/except means a missing index gives you a server that starts and reports
its problem, instead of a crash loop with a stack trace. Compare the two
experiences when you are on call at 3am.

```python
@app.get("/health")
def health():
    return {"status": "ok" if _retriever and _chunk_count else "degraded",
            "vector_store": ..., "chunks": _chunk_count,
            "index": "ready" if ... else "empty or unavailable",
            "tracing": "on" if is_tracing_enabled() else "off"}
```

A health check that reports **`degraded`** rather than just up/down, and includes
the chunk count and which backend is live. A load balancer can act on it and a
human can debug with it.

```python
@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest) -> AskResponse:
    if _retriever is None:
        raise HTTPException(status_code=503, detail="Vector store unavailable or empty. "
                            "Run: python -m src.ingestion.pipeline")
    result = run_agent(body.question, retriever=_retriever)
    return AskResponse(**result)
```

- **503** (Service Unavailable) is the correct code — the request was fine, the
  server is not ready — and the message says how to fix it.
- `AskRequest`/`AskResponse` are pydantic models, so FastAPI validates input
  (`min_length=3` rejects `"hi"`), serialises output, and generates the
  OpenAPI docs at `/docs` from these types. The interactive docs page is free.

Two honest observations to keep in your pocket: the endpoint is synchronous
(`def`, not `async def`) so FastAPI runs it in a threadpool — fine, and correct
given `run_agent` is blocking; and there is no test file covering this layer,
which is the most obvious gap in the test suite (chapter 11).

## Say this out loud

> "Three nodes. Route is an LLM call that picks 1–3 sources and optional
> sub-queries, with JSON parsing that falls back twice and validation against the
> enum, so a bad routing response degrades to plain RAG instead of failing.
> Retrieve runs the sub-queries concurrently — they're I/O bound — and dedupes in
> input order so results are deterministic. Synthesize refuses without evidence
> before spending an LLM call, otherwise prompts with numbered evidence and a
> conflict-precedence rule, then filters the citation list to the markers the
> answer actually used. The retriever is built once at app startup, and a missing
> index gives a 503 with instructions instead of a crash."

## Prove it to yourself

```python
from src.agent.nodes import route_sources
for q in ["Who owns Payments?",
          "How do I create a capability and are there open tickets about it?"]:
    s = route_sources({"question": q})
    print(q, "->", s["selected_sources"], "|", s["sub_queries"])
```

Watch the second question get decomposed. Then break the router deliberately:
temporarily make `_parse_json` return `{}` and confirm `/ask` still answers using
the `devportal + city_map` fallback. That is graceful degradation, demonstrated
rather than claimed.
