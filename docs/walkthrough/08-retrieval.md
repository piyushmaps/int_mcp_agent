# 8 — Retrieval: finding evidence, and knowing when there is none

**File:** [`src/retrieval/retriever.py`](../../src/retrieval/retriever.py) — 92
lines, and the shortest chapter. Its brevity is the lesson.

## 8.1 What is left after the backends took their share

Read the module docstring first — it is unusually direct about its own history:

> This module used to carry FAISS's limitations — an escalating over-fetch window
> to work around the lack of metadata filtering. That workaround now lives inside
> the FAISS backend, where it belongs, and OpenSearch does not need it at all.
> What is left here is the part that is genuinely retrieval **policy** rather
> than storage **mechanics**.

That distinction is the whole design of this layer:

- **Mechanics** — *how* a store finds neighbours, whether it can filter, how it
  deletes. Backend's problem (chapter 6).
- **Policy** — *what we ask for* and *what we accept*. This file's problem.

Three lines of policy, and that is all:

```python
vector = self.embeddings.embed_query(query)
hits = self.store.search(vector, k=k, sources=source_values, query_text=query)
hits = [h for h in hits if h.score >= min_similarity]
```

Embed the question. Ask for the routed sources. Drop what is not close enough.

## 8.2 The constructor and dependency injection

```python
def __init__(self, store: VectorStore | None = None, embeddings: OpenAIEmbeddings | None = None):
    settings = get_settings()
    self.store = store if store is not None else get_vector_store(settings)
    self.embeddings = embeddings or OpenAIEmbeddings(...)
```

Both collaborators can be passed in, but default to the real thing. That is
**dependency injection**, and it is why `tests/test_retriever.py` can hand in a
`StubStore` and a `StubEmbeddings` and test the policy with no network, no index
and no API key. It is also how the API server reuses one retriever across
requests instead of rebuilding it per call.

Note `store if store is not None else ...` rather than `store or ...`. Subtle but
correct: an empty store might be falsy under some `__len__`/`__bool__`
implementation, and `or` would silently discard it. The `embeddings or ...` on
the next line is the looser form. If you wanted one nit to fix in this file, that
inconsistency is it.

`RetrievedChunk` is a small dataclass — content, source, title, url, score. It is
deliberately *not* the store's `Chunk`: what leaves retrieval is what the agent
and the citation renderer need, nothing more. A boundary type.

## 8.3 The cutoff — the most important five lines in the project

```python
# A vector index has no notion of "no match" — it returns the k nearest
# neighbours however distant. Without this, an off-corpus question comes
# back with a full set of citations that support nothing.
if min_similarity is not None:
    hits = [h for h in hits if h.score >= min_similarity]
```

Internalise the first sentence. A vector index **always** returns *k* results. Ask
"what is the capital of France?" against a corpus about capability registration,
and you still get six chunks — the six *least dissimilar* ones. They are about
nothing relevant, but they come back with the same confidence as a perfect match.

Feed those to an LLM with "answer using this evidence" and it will produce
something plausible and wrong, decorated with six citations. That is the failure
mode people mean when they say RAG systems hallucinate — and it is not the LLM's
fault, it is the missing threshold.

`min_similarity` defaults to 0.28, and the config comment records the measurement
that justifies it:

```
# Minimum cosine similarity for a chunk to count as relevant. Measured on
# the sample corpus: on-topic questions score 0.69-0.85, off-topic 0.09 and
# below. Scores are cosine everywhere, so this means the same in any backend.
```

On-topic 0.69–0.85, off-topic ≤0.09, cutoff 0.28 — sitting in a wide empty gap,
far from both clusters. **That is how a threshold should be chosen: measured,
recorded, and placed where small errors do not change the outcome.** "0.28
because it's cosine and the gap is 0.09 to 0.69" is a real engineering answer;
"0.28 felt right" is not.

Two honest caveats you should volunteer rather than be caught on:

1. It was measured on *this* corpus. A different corpus needs re-measuring.
2. The last sentence of that comment is load-bearing and depends entirely on the
   chapter 6 invariant. The moment any backend returns a non-cosine score, this
   number silently stops meaning anything — which is exactly the bug that was
   fixed in the OpenSearch BM25 branch.

And notice the payoff arrives in the *next* stage: when retrieval returns
nothing, [`synthesize_answer`](../../src/agent/nodes.py#L109-L119) short-circuits
to "I couldn't find relevant evidence" **before** spending an LLM call. Honest
refusal is cheaper than a wrong answer, in both money and trust.

## 8.4 The sentinel

```python
def retrieve(self, query, sources=None, k=None, min_similarity: float | None = -2.0):
    if min_similarity == -2.0:
        min_similarity = self.min_similarity
```

This looks odd, so understand the problem it solves. The parameter needs **three**
states:

- caller said nothing → use the configured default
- caller passed a number → use that number
- caller passed `None` → **disable the cutoff entirely**

The usual default (`None`) is already taken by the third meaning. So a sentinel
value stands for "unspecified", and `-2.0` is chosen because cosine similarity
lives in [-1, 1] — no real score can ever be -2.0, so it is unambiguous. The
docstring says exactly that.

(The mainstream alternative is a module-level `_UNSET = object()` sentinel, which
is more idiomatic but less self-explaining in a signature. Either is defensible;
what matters is that the choice is deliberate and documented.)

## 8.5 Source values

```python
source_values = (
    [s.value if isinstance(s, SourceType) else str(s) for s in sources]
    if sources else None
)
```

Callers may pass `SourceType` enums or plain strings (the router returns
strings). This normalises both to strings before they reach the store — because
`VectorStore.search` takes `Iterable[str]`, and the backend should never have to
know your enum type exists. Small thing, but it is the same boundary discipline
as everywhere else: convert at the edge, keep the inside uniform.

## Say this out loud

> "The retriever holds policy only: embed the query, ask the store for the routed
> sources, and drop anything below the cosine cutoff. Storage mechanics live in
> the backends. The cutoff is what lets the system say 'I don't know' — a vector
> index always returns k neighbours however distant, so without a threshold an
> off-corpus question comes back with citations that support nothing. 0.28 was
> chosen by measuring the gap between on-topic scores of 0.69 to 0.85 and
> off-topic scores below 0.09."

## Prove it to yourself

```python
from src.retrieval.retriever import SourceRetriever
r = SourceRetriever()

for q in ["Who owns the Payments capability?", "What is the capital of France?"]:
    hits = r.retrieve(q, min_similarity=None)     # cutoff disabled: see raw scores
    print(q)
    for h in hits[:3]:
        print(f"   {h.score:.3f}  {h.source:<10} {h.title[:50]}")
    print("   with cutoff:", len(r.retrieve(q)), "hits\n")
```

You will see the on-topic question keep its hits and the off-topic one drop to
zero — and, crucially, you will see the off-topic question's *raw* neighbours,
which is the thing the system would otherwise have cited. Then read
`tests/test_retriever.py`: seven tests, and they are all about this one policy.
