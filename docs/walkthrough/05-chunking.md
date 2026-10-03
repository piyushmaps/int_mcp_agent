# 5 — Chunking: where to cut, and where not to

**File:** [`src/ingestion/chunking.py`](../../src/ingestion/chunking.py) — 73 lines.

## Why cut documents at all

Two reasons, and only the second one is really about quality.

**1. Limits.** Embedding models take a bounded amount of text. A 300-page PDF
does not fit.

**2. Precision — the real reason.** An embedding is *one* vector for whatever
text you give it. Embed a 300-page book and you get a single point that means
"calculus, vaguely". It will never be the nearest neighbour for
"what is the chain rule?", because the question is specific and the vector is an
average of everything.

Retrieval quality is bounded by chunk quality. A chunk should be **one idea**:
big enough to stand alone, small enough to be about something.

The unit produced here is `Chunk`, defined in
[`vectorstore/base.py:16-31`](../../src/vectorstore/base.py#L16-L31) — text plus
everything needed to cite it.

## Reading the file

```python
KEEP_WHOLE_SOURCES = {SourceType.SLACK, SourceType.JIRA, SourceType.CITY_MAP}
```

Chunking here is **source-aware**, not one-size-fits-all. Hold that thought.

```python
def _splitter() -> RecursiveCharacterTextSplitter:
    settings = get_settings()
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,       # 800
        chunk_overlap=settings.chunk_overlap, # 120
        separators=["\n## ", "\n# ", "\n\n", "\n", " "],
    )
```

**How "recursive" splitting works.** The splitter tries the first separator. If
the resulting pieces are still bigger than `chunk_size`, it takes those pieces
and tries the next separator, and so on down the list. So it splits on `##`
headings if it can; falls back to `#` headings; then blank lines (paragraphs);
then single newlines; and only as a last resort on spaces (mid-sentence).

The order of that list *is* the priority order of cut points. Chapter 4 showed
`extract_html` deliberately emitting `#`/`##` markers so this list has something
good to find — and DevPortal Markdown carries them natively. The chunker and the
extractors are cooperating across two files.

**Overlap of 120 characters** means consecutive chunks share their edges. Without
it, a sentence split across a boundary is in neither chunk as a whole thought,
and a question matching that sentence matches nothing well. The cost is about
15% duplicated text. That is the standard trade and 800/120 is a reasonable
default, not a tuned result — say that honestly if asked.

### The keep-whole rule

```python
if doc.source in KEEP_WHOLE_SOURCES and len(text) <= keep_whole_limit:
    yield Chunk(id=f"{doc.id}#0", content=text, ...)
    continue
```

Slack threads, Jira tickets, and city map records that already fit inside
`chunk_size` are indexed as **one chunk each**, untouched.

Why: these documents are *already* the right unit. A five-message Slack thread is
one Q&A exchange; cutting it in half puts the question in one chunk and the
answer in another, and retrieving either alone is useless. Same for a ticket
(description and its comments belong together) and a city map row (an ownership
record is meaningless as fragments).

Long DevPortal guides are the opposite — a 4,000-word lifecycle doc genuinely
contains many separate ideas, so it gets split.

Note the rule has **two conditions**. A source in the keep-whole set that is
*longer* than `chunk_size` still gets split — otherwise a huge Jira ticket would
be one oversized chunk. The set expresses a preference, not an absolute.

### Metadata carried onto every chunk

```python
base = {
    "doc_id": doc.id,
    "source": doc.source.value,
    "title": doc.title,
    "url": doc.url,
}
scalar_meta = {
    k: v for k, v in doc.metadata.items() if isinstance(v, str | int | float | bool)
}
```

`base` is the four fields every chunk needs: `doc_id` to group and delete chunks
by document, `source` to filter at query time, and `title` + `url` to render a
citation. A search hit has to be able to answer "where did this come from?"
entirely on its own, without a lookup back to the original document — because at
query time the `Document` objects no longer exist anywhere.

The `scalar_meta` filter keeps only simple values — strings, numbers, booleans.
Jira's `labels` (a list) is dropped here. The reason is that this metadata is
stored in a search index and, for the FAISS backend, serialised to JSON on disk;
restricting it to scalars keeps that predictable and keeps nested structures out
of the index mapping. It is a small defensive narrowing, and it is why you should
not assume every key you set in a connector survives to query time.

`isinstance(v, str | int | float | bool)` is the 3.10+ way of writing
`isinstance(v, (str, int, float, bool))`.

### Deterministic chunk ids

```python
yield Chunk(id=f"{doc.id}#{position}", ...)
```

The id of a chunk is its document id plus its position: `devportal:create-capability#3`.

This one line buys a lot. Re-ingest a changed document and its chunk 3
**overwrites** the previous chunk 3, because both stores treat writing an
existing id as a replace. Without deterministic ids you would either accumulate
duplicates every run or have to delete-then-insert everything.

It has one gap, and the pipeline knows about it: if the *new* version of a
document produces fewer chunks (say 3 instead of 5), chunks `#3` and `#4` from
the old version are still in the index — orphans, citing a paragraph that no
longer exists. That is why
[`pipeline.py:129-133`](../../src/ingestion/pipeline.py#L129-L133) explicitly
calls `delete_docs` before upserting an updated document. Chapter 7.

```python
def chunk_document(doc: Document) -> list[Chunk]:
    return list(iter_chunks([doc]))
```

The pipeline needs the chunk *count* for one document before writing it to the
manifest, so it takes the materialised list. One document's chunks in memory is
fine; the whole corpus would not be.

## Say this out loud

> "Chunking is source-aware. Long docs are split at 800 characters with 120 of
> overlap, preferring Markdown headings so chunks land on section boundaries.
> Slack threads, Jira tickets and city map records that already fit are kept
> whole, because splitting a conversation destroys the context that makes it
> retrievable. Every chunk carries source, title and URL so a hit can cite
> itself, and chunk ids are `doc_id#position` so re-ingesting overwrites instead
> of duplicating."

## Prove it to yourself

```python
from src.connectors import DevPortalConnector, SlackConnector
from src.ingestion.chunking import chunk_document

doc = DevPortalConnector().load()[1]      # create-capability.md, the longer one
chunks = chunk_document(doc)
print(len(chunks))                        # 3
for c in chunks[:3]:
    print(c.id, "|", len(c.content), "|", c.content[:70].replace("\n", " "))

print([c.id for c in chunk_document(SlackConnector().load()[0])])  # one id, ending #0
# and note capability-lifecycle.md is 791 chars -> also 1 chunk, just under the 800 limit
```

Then experiment: set `chunk_size=200` in `.env`, re-run the first block, and read
a chunk. Notice how a 200-character chunk is often half a thought — that is what
"chunk too small" feels like, and why the number matters.
