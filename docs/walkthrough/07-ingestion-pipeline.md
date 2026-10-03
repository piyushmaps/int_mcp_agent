# 7 — The ingestion pipeline: a job that survives reality

**Files:** [`pipeline.py`](../../src/ingestion/pipeline.py) (245 lines) and
[`manifest.py`](../../src/ingestion/manifest.py) (115 lines).

Everything so far has been a piece. This is the program that assembles them:

```
connectors → documents → hash → chunk → embed → vector store
                          ↓ unchanged?
                       skipped
```

The naive version of this file is 30 lines: load all documents, chunk them all,
embed them all, write them all. It works on sample data and falls apart on
anything real. The four properties below are what the extra 200 lines buy, and
being able to name them is the point of this chapter.

## 7.1 The four properties

| Property | What it means | Where it comes from |
|---|---|---|
| **Streaming** | Peak memory is set by batch size, not corpus size | generators end to end |
| **Incremental** | Unchanged documents are never re-embedded — second run costs $0 | content hash in the manifest |
| **Resumable** | A run killed at 70% restarts at 70% | progress committed as batches land |
| **Isolated failures** | A corrupt file costs that file, not the job | try/except in the connector |

Notice that **resumability is not a feature that was built** — it falls out of
incrementality. If unchanged documents are skipped, then a re-run after a crash
automatically skips everything already done. One mechanism, two properties. That
is the kind of thing worth pointing at when someone asks what you designed.

## 7.2 The manifest — one small table that does all the work

[`manifest.py`](../../src/ingestion/manifest.py). SQLite, one table:

```sql
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    source       TEXT NOT NULL,
    chunk_count  INTEGER NOT NULL,
    indexed_at   TEXT NOT NULL
);
```

```python
def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

A **hash** is a fixed-length fingerprint of a piece of text. Same text → same
fingerprint; any edit → a completely different one. So "has this document
changed?" becomes a string comparison instead of a re-embed.

```python
self._known: dict[str, str] = {
    row[0]: row[1] for row in self.conn.execute("SELECT doc_id, content_hash FROM documents")
}
```

The whole table is read into a dictionary once at startup, so the per-document
check `is_unchanged()` is an in-memory lookup rather than a SQL query. With a
million documents that dict is maybe 150 MB — acceptable, and worth knowing as
the point where you would switch to querying SQLite directly.

```python
"INSERT INTO documents (...) VALUES (?, ?, ?, ?, ?) ON CONFLICT(doc_id) DO UPDATE SET ..."
```

An **upsert**: insert, or update if that `doc_id` already exists. One statement
handles both new and changed documents. The `?` placeholders are parameterised
values — the habit that prevents SQL injection; keep it even in a local file.

```python
def vanished(self, seen: set[str]) -> list[str]:
    return [doc_id for doc_id in self._known if doc_id not in seen]
```

Documents the manifest remembers that this run did *not* produce — i.e. deleted
at the source. Without this, your index answers questions from a doc that was
deleted six months ago, and the citation 404s. The docstring carries the critical
caveat: **only meaningful after a full run.** A partial run would call everything
it had not reached yet "deleted".

```python
@contextmanager
def open_manifest(path: Path) -> Iterator[IngestionManifest]:
    manifest = IngestionManifest(path)
    try:
        yield manifest
    finally:
        manifest.close()
```

A context manager, so `with open_manifest(...) as manifest:` guarantees the
final commit and the connection close even if the body raises. Same pattern as
`with open(...) as f`.

One more line worth understanding:

```python
self.conn.execute("PRAGMA synchronous=NORMAL")
```

SQLite normally forces a disk sync on every commit — safe, slow. `NORMAL`
relaxes that. The comment explains the reasoning: the manifest is *rebuildable*
by re-running ingestion, which is idempotent anyway. The worst case is
re-embedding a batch. That is a deliberate durability-for-speed trade with a
stated justification, which is what separates a decision from a shortcut.

## 7.3 The main loop

[`run_ingestion`](../../src/ingestion/pipeline.py#L177-L231):

```python
for doc in iter_all_documents(connectors):
    stats.documents_seen += 1
    seen.add(doc.id)
    digest = content_hash(doc.to_text())

    known = not full_refresh and manifest.is_unchanged(doc.id, digest)
    if known:
        stats.documents_skipped += 1
        continue

    is_update = manifest.knows(doc.id)
    chunks = chunk_document(doc)
    if not chunks:
        continue
    stats.documents_indexed += 1
    batcher.add(doc, digest, chunks, is_update)
```

Read it as five questions per document:

1. **Have I seen it?** `seen.add(doc.id)` — needed later for pruning.
2. **What is it now?** hash of `to_text()` (chapter 2's single definition of
   content — this is why that method matters).
3. **Has it changed?** If not, skip. This is the line that makes a second run
   free. `--full-refresh` forces the answer to "yes, everything changed".
4. **Is it new or an update?** An update needs its old chunks deleted (chapter 5's
   orphan problem).
5. **Chunk it and hand it to the batcher.**

Note the whole loop never holds more than one document. `iter_all_documents` is
itself a generator chaining the four connectors' generators.

Then, after the loop:

```python
if prune:
    gone = manifest.vanished(seen)
    if gone:
        stats.documents_deleted = store.delete_docs(gone)
        manifest.forget(gone)
```

Deletions, guarded by the `prune` flag, exposed as `--no-prune`. The comment
states the rule: *only safe after a complete pass*. If you ever add "ingest just
the Jira source", it **must** pass `prune=False`, or it will delete every Slack
and DevPortal document from the index. That is the sharpest edge in this file.

## 7.4 The batcher

Why batch at all? Embedding is a network call. Sending 256 chunks in one request
instead of 256 requests is roughly two orders of magnitude less overhead.

[`_Batcher`](../../src/ingestion/pipeline.py#L114-L175):

```python
def add(self, doc, digest, chunks, is_update):
    if is_update:
        self.replaced.append(doc.id)
    self.chunks.extend(chunks)
    self.pending.append((doc.id, digest, doc.source.value, len(chunks)))
    if len(self.chunks) >= self.settings.embed_batch_size:
        self.flush()
```

Accumulate until the batch is full, then flush. `pending` holds manifest rows
that must *not* be written until the chunks are actually in the store — write
them earlier and a crash would leave the manifest claiming a document is indexed
when it is not, and the next run would skip it forever. **Order matters here**,
and `flush()` gets it right:

```python
vectors = _embed(self.embeddings, [c.content for c in self.chunks])
if not self.ready:
    self.store.ensure_ready(len(vectors[0]))
    self.ready = True
if self.replaced:
    self.store.delete_docs(self.replaced)
written = self.store.upsert(self.chunks, vectors)
...
self._commit_pending()          # manifest updated only after the write succeeded
```

embed → create index if needed → delete old versions → write → *then* record.

`ensure_ready(len(vectors[0]))` is neat: the index needs to know the embedding
dimension, and rather than hard-coding 1536 the code reads it off the first
vector it actually received. Change the embedding model and nothing here breaks.

```python
if self.stats.batches % self.settings.checkpoint_every_batches == 0:
    self.store.persist()
```

Every 20 batches, flush the index to durable storage. This is the resumability
knob: crash after batch 40 and you have lost at most the work since the last
checkpoint. More frequent checkpoints mean less lost work and more I/O.

## 7.5 Retry policy — and the one place failure is fatal

```python
for attempt in range(1, EMBED_MAX_ATTEMPTS + 1):
    try:
        return embeddings.embed_documents(texts)
    except Exception as exc:
        if attempt == EMBED_MAX_ATTEMPTS:
            logger.error("embedding failed after %d attempts: %s", attempt, exc)
            raise
        delay = 2**attempt
        logger.warning("embedding attempt %d failed (%s); retrying in %ds", attempt, exc, delay)
        time.sleep(delay)
```

**Exponential backoff**: wait 2s, then 4s, then 8s. Retrying immediately hammers
a service that is already rate-limiting you; backing off gives it room. Four
attempts, then `raise`.

That `raise` is the deliberate part, and it contrasts with chapter 4's
skip-and-continue:

> A corrupt file is logged and skipped. A failing embedding API is fatal on
> purpose: better to stop than to quietly index a partial corpus, and the
> manifest makes the retry cheap.

Think about why they differ. A corrupt PDF is a *local* problem — one file is
bad, the other 4,000 are fine. A failing embedding API is a *systemic* problem —
nothing more will succeed. Continuing would produce an index that looks complete
and silently isn't, which is the worst outcome: it answers questions wrongly with
no error anywhere. And because the manifest recorded everything up to the
failure, restarting costs only the unfinished part. **The recovery mechanism is
what makes the strict failure affordable.**

The unreachable `raise AssertionError("unreachable")` after the loop exists to
satisfy type checkers, which cannot see that the loop always returns or raises.

## 7.6 Reporting

`IngestionStats` counts documents seen / skipped / indexed, chunks written,
documents deleted, batches, failures, and elapsed time, and `render()` prints
them with a chunks-per-second rate. `main()` prints the first 10 skipped files
with reasons.

This is not decoration. "Ingestion finished" tells you nothing; "4,577 seen,
4,201 skipped, 376 indexed, 2 failures" tells you whether the run did what you
expected. Being able to see a job is the difference between one you can operate
and one you can only hope about.

## Say this out loud

> "Ingestion is streaming end to end, so memory is bounded by the batch, not the
> corpus. A SQLite manifest fingerprints each document, so unchanged ones are
> never re-embedded — which also makes the job resumable for free, and makes
> deleted documents detectable. Chunks are batched 256 at a time for embedding,
> the manifest is only written after the store write succeeds, and the index is
> checkpointed every 20 batches. Extraction failures are skipped; embedding
> failures retry with backoff and then abort, because a silently partial index is
> worse than a stopped job."

## Prove it to yourself

1. `python -m src.ingestion.pipeline` twice. Second run: everything "unchanged
   (skipped)", near-zero time, no API cost.
2. Edit one line of `data/sample/devportal/create-capability.md` and run again.
   Exactly one document is re-indexed. That is the content hash working.
3. `sqlite3 data/index/manifest.sqlite "select source, count(*) from documents group by source;"`
   — or use `IngestionManifest(...).stats()`.
4. Rename a sample file and run again. Watch `documents deleted` become non-zero:
   the old id vanished, so pruning removed it.
5. Read `tests/test_pipeline.py` — eight tests, each asserting one property from
   the table in 7.1. Tests as executable documentation.
