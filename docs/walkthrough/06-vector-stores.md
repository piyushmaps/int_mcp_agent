# 6 — Vector stores: where the meaning lives

**Files:** [`base.py`](../../src/vectorstore/base.py) (the contract),
[`factory.py`](../../src/vectorstore/factory.py) (the switch),
[`faiss_store.py`](../../src/vectorstore/faiss_store.py) (local),
[`opensearch.py`](../../src/vectorstore/opensearch.py) (scale).

This is the most technical chapter. Take it in two sittings if you need to: 6.1–6.3
are the ideas, 6.4–6.5 are the two implementations.

## 6.1 Cosine similarity, without hand-waving

An embedding is a list of ~1536 numbers — a point in 1536-dimensional space, or
equivalently an arrow from the origin to that point. What carries meaning is the
arrow's **direction**, not its length.

So similarity is measured as the cosine of the angle between two arrows:

- Same direction (angle 0) → cosine **1.0** → same meaning.
- Perpendicular (90°) → cosine **0.0** → unrelated.
- Opposite (180°) → cosine **-1.0**.

Cosine is computed as `dot(a, b) / (|a| * |b|)`. If both vectors are already
**normalised** to length 1, the denominator is 1 and cosine is just the dot
product. That is why normalisation shows up everywhere in this code — it turns an
expensive-ish formula into a plain dot product, which is what the index hardware
is optimised for.

## 6.2 The contract, and the one invariant

```python
@runtime_checkable
class VectorStore(Protocol):
    def ensure_ready(self, dim: int) -> None: ...
    def upsert(self, chunks, vectors) -> int: ...
    def delete_docs(self, doc_ids) -> int: ...
    def search(self, vector, k, sources=None, query_text=None) -> list[SearchHit]: ...
    def count(self) -> int: ...
    def persist(self) -> None: ...
```

**`Protocol` is not `ABC`.** With an ABC (chapter 3) a class must *inherit* to
qualify. With a Protocol, any class that has these methods qualifies — no
inheritance, no import, no registration. This is called structural typing, and
it is why `tests/test_retriever.py` can define a 12-line `StubStore` that is not
related to anything and hand it straight to the retriever. Type checkers accept
it; the runtime never cared. Notice neither `FaissVectorStore` nor
`OpenSearchVectorStore` inherits from `VectorStore` either.

Six methods. Small on purpose — the docstring says why:

> Deliberately small: anything a backend cannot do natively (metadata filtering,
> deletes) has to be solved by the backend, not leaked upward as a workaround the
> callers have to know about.

That sentence is a design rule the project actually follows, and section 6.4 is
the proof.

**Now the invariant that everything else depends on:**

> `SearchHit.score` is **cosine similarity**, in [-1, 1], higher is better, and it
> means the same thing in every backend.

FAISS natively returns an L2 distance (lower is better). OpenSearch returns its
own scaled score. Both are converted *inside their own module* so callers never
branch on which store is behind them. The moment that invariant breaks, the
relevance cutoff in chapter 8 becomes meaningless — and there is a real fixed bug
in this repo (6.5) that was exactly that.

```python
def l2_to_cosine(distance: float) -> float:
    return 1.0 - (distance * distance) / 2.0
```

The derivation, since you should be able to reproduce it: for unit vectors,
|a−b|² = |a|² + |b|² − 2·a·b = 1 + 1 − 2cos = 2 − 2cos. Rearranged,
cos = 1 − |a−b|²/2. That is the line.

## 6.3 The factory

[`factory.py`](../../src/vectorstore/factory.py) is the only place that knows
which backend is in use:

```python
backend = settings.vector_store.strip().lower()
if backend == "opensearch":
    return _opensearch(settings)
if backend == "faiss":
    from src.vectorstore.faiss_store import FaissVectorStore
    return FaissVectorStore(ROOT_DIR / settings.faiss_index_path)
raise ValueError(f"Unknown VECTOR_STORE={backend!r}; expected 'faiss' or 'opensearch'.")
```

Two details worth copying into your own habits:

- **Imports are inside the branches.** Importing `faiss` or `opensearchpy` is
  slow and requires the package to be installed. Deferring the import means a
  FAISS user never loads OpenSearch and vice versa.
- **The unknown value raises with the value in the message.** `{backend!r}`
  prints it with quotes, so a trailing space or wrong case is visible. Compare
  with silently defaulting to FAISS: you would deploy believing you were on
  OpenSearch. Failing loudly on bad configuration is the right call.

`ROOT_DIR / settings.faiss_index_path` makes the relative path in `.env` absolute
against the repo root, so the index location does not depend on the working
directory you happened to launch from.

## 6.4 FAISS — the local one

FAISS is a library, not a server. It holds vectors in memory and searches them
fast. `pip install` and you are done — which is why it is the default.

**Creating the index:**

```python
self.index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
```

- `IndexFlatIP` — "flat" means it stores every vector and compares against all of
  them on each query (exact, no approximation). `IP` = inner product. Combined
  with normalised vectors, inner product *is* cosine, so no conversion is needed
  for this backend.
- `IndexIDMap2` wraps it to allow your own integer ids and **delete by id**,
  which plain flat indexes do not support.

Flat means exact results and no tuning, at the cost of scanning everything. Fine
for tens of thousands of chunks, a wall at tens of millions. That is the honest
boundary where OpenSearch takes over.

**Writing:**

```python
replaced = [self.by_chunk_id[c.id] for c in chunks if c.id in self.by_chunk_id]
if replaced:
    self._remove(replaced)
ids = np.arange(self._next_id, self._next_id + len(chunks), dtype=np.int64)
self.index.add_with_ids(_normalize(vectors), ids)
```

FAISS wants integer ids; chunks have string ids. So the store keeps two
dictionaries — `chunks` (int → Chunk) and `by_chunk_id` (str → int) — and a
counter. Re-writing an existing chunk id removes the old vector first, so
`upsert` means the same thing here as it does in OpenSearch. Making two very
different backends behave identically is most of the work in a plugin interface.

`_normalize` calls `faiss.normalize_L2`, in place, on a float32 numpy array.

**Persisting** writes two files: `index.faiss` (the vectors, FAISS's own binary
format) and `chunks.json` (the text and metadata, plus `next_id`). FAISS stores
only vectors; the payload has to live beside it. On startup `_load` reads both
back and rebuilds `by_chunk_id`. Note the consequence: the entire chunk text of
the corpus is one JSON file loaded into memory — another reason this backend is
the small-scale option.

**The escalating search window** — the most interesting code in the file:

```python
if wanted is None:
    return self._top(query, k, None)

fetch = min(k * FETCH_ESCALATION, self.index.ntotal)
while True:
    hits = self._top(query, fetch, wanted)[:k]
    if len(hits) >= k or fetch >= self.index.ntotal:
        return hits
    fetch = min(fetch * FETCH_ESCALATION, self.index.ntotal)
```

FAISS has **no metadata filtering**. It cannot be told "only DevPortal chunks".
So the filter has to happen *after* the search: fetch some results, throw away
the ones from the wrong source, keep what remains.

The naive version — fetch `k * 3`, filter, return — has a nasty failure mode. If
DevPortal is 2% of the corpus and you ask for the top 18 overall, maybe zero of
them are DevPortal. You would get **nothing back**, and the symptom would be
"narrowing the route made retrieval worse", which is the exact opposite of what
routing is for. Worse, it fails quietly: no error, just a thin answer.

So the window *escalates*: ×3 each round until either `k` matching hits are found
or the whole index has been scanned. Bounded by `ntotal`, so it always
terminates. Worked example on a 10,000-chunk index, `k=6`: try 18 → 2 hits; try
54 → 5; try 162 → 6, return.

**And this is the design rule from 6.2 in action.** That loop is a workaround for
one backend's limitation, and it lives *inside that backend*. The retriever
(chapter 8) does not know it exists; OpenSearch does not pay for it. The
commit that moved it here is `f244285 refactor: retriever keeps policy, backends
keep their own workarounds` — that is the kind of change you should be able to
defend.

## 6.5 OpenSearch — the one built for scale

Three things it does that FAISS cannot, all of which stop being optional as the
corpus grows.

**1. Filters inside the query.**

```python
knn = {"vector": list(vector), "k": k}
if source_filter:
    knn["filter"] = source_filter          # {"terms": {"source": [...]}}
body = {"size": k, "query": {"knn": {"embedding": knn}}}
```

The engine applies the source filter *during* the search. The whole escalating
window problem disappears.

Look at the index mapping in `ensure_ready`. `source`, `doc_id`, `url` are typed
`keyword` while `content` and `title` are `text`. In Elasticsearch/OpenSearch,
`text` is broken into words for search; `keyword` is stored whole for exact
matching, filtering and sorting. You filter on `keyword` fields and search on
`text` fields — getting that backwards is a classic beginner bug.

The vector field:

```python
"embedding": {"type": "knn_vector", "dimension": dim,
              "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"}}
```

**HNSW** is an approximate nearest-neighbour algorithm: a navigable graph you
walk toward the query instead of scanning everything. It gives up exactness for
speed that does not collapse at scale. `ef_search` (default 100, configurable)
controls how wide the walk is — higher is more accurate and slower. That
accuracy/latency dial is one of the differences between "a library" and "a search
engine".

**2. Deletes and updates by document.** `_op_type: "index"` with an explicit
`_id` is an upsert; `delete_by_query` on `doc_id` removes every chunk of a
document server-side. Nothing is rebuilt, nothing else is touched.

**3. Hybrid search — keyword *and* vector.**

Semantic search has a real blind spot: exact tokens. Ask about `CAP-142` and the
embedding of a ticket key carries almost no meaning — every ticket key looks
alike to a language model. Old-fashioned keyword search (**BM25**) is excellent
at exactly that. So both are run and their rankings are merged.

```python
def _fuse(primary, secondary, k):
    for ranking in (primary, secondary):
        for rank, hit in enumerate(ranking, start=1):
            contribution[cid] = contribution.get(cid, 0.0) + 1.0 / (RRF_K + rank)
```

This is **reciprocal rank fusion**. Each list contributes `1/(60 + rank)` to each
chunk it contains, and the totals are sorted. Rank 1 contributes 1/61 ≈ 0.0164,
rank 2 contributes 1/62 ≈ 0.0161, and so on.

Why fuse on *rank* rather than score? Because BM25 scores and cosine scores live
on incomparable scales — a BM25 score of 12.7 and a cosine of 0.81 cannot be
averaged meaningfully, and any attempt needs normalisation constants that you
would have to re-tune per corpus. Ranks are always comparable. The effect of the
formula is that a chunk **both** rankings like beats a chunk that only one
ranking loves — which is usually the right instinct. `RRF_K = 60` is the constant
from the original paper; it flattens the difference between adjacent top ranks so
that appearing in both lists matters more than being #1 in one.

**Now the subtle bug that was fixed here — understand this one properly.**

```python
hits = self.client.search(...)          # BM25 query
return [
    SearchHit(chunk=_to_chunk(h), score=_cosine(vector, h["_source"].get("embedding")))
    for h in hits["hits"]["hits"]
]
```

The BM25 branch throws away BM25's own score and recomputes cosine similarity
against the stored embedding. Why? Because of the invariant in 6.2. BM25 scores
are unbounded — they can be 12 or 40 — and the retriever's relevance cutoff
(chapter 8) compares scores against 0.28. A keyword-only hit carrying a BM25
score of 12 sails straight past that cutoff. The symptom: ask a question about
nothing in the corpus, share one common word with some chunk, and the system
answers confidently with a citation that supports nothing. That is a *credibility*
bug, not a performance one.

The fix keeps BM25 for **ranking** (it still decides the order of that list) but
restates the **score** in the one unit everything downstream understands. The
commit is `cd9c7e7 fix: keyword-only hits must not carry BM25 scores past the
relevance cutoff`. If you only remember one bug from this project, remember this
one — it shows why the "score means one thing everywhere" invariant is worth
enforcing.

```python
def _cosine_from_score(score: float) -> float:
    return 2.0 * float(score) - 1.0
```

OpenSearch's `cosinesimil` space reports similarity squeezed into [0, 1]; this
undoes that mapping back to [-1, 1]. And the docstring says it was **verified
against a live cluster** rather than trusted from documentation — see
`tests/test_opensearch_store.py::test_score_is_cosine_similarity`, which indexes
vectors at known angles and asserts the returned scores match the cosines. That
test is the reason you can trust every threshold downstream.

## Say this out loud

> "The `VectorStore` protocol is six methods, and one invariant: score is cosine
> similarity, higher is better, in every backend. FAISS is a library — exact
> flat search, everything in memory, no metadata filtering, so source filtering
> is a post-filter with an escalating window that lives inside the FAISS module.
> OpenSearch is a server — HNSW approximate search, filters inside the query,
> delete by document, and hybrid BM25+vector retrieval fused by reciprocal rank.
> Keyword hits get their cosine recomputed so they cannot smuggle an unbounded
> BM25 score past the relevance cutoff."

## Prove it to yourself

```python
from pathlib import Path
from src.vectorstore.faiss_store import FaissVectorStore
from src.vectorstore.base import Chunk

s = FaissVectorStore(Path(".scratch-demo-index"))   # nothing is written unless you persist()
s.ensure_ready(2)
s.upsert([Chunk(id="a#0", doc_id="a", content="x", source="jira", title="A", url="")],
         [[1.0, 0.0]])
print(s.search([1.0, 0.0], k=1)[0].score)   # ~1.0  (same direction)
print(s.search([0.0, 1.0], k=1)[0].score)   # ~0.0  (perpendicular)
```

Two vectors, and you have felt cosine similarity directly. Then run
`pytest tests/test_faiss_store.py -v` and read the test names — they are a list
of the guarantees this backend makes.
