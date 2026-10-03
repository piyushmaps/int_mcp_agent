# 3 — Connectors: turning four worlds into one

**Files:** [`base.py`](../../src/connectors/base.py) (the contract — the file you
had open), [`jira.py`](../../src/connectors/jira.py),
[`slack.py`](../../src/connectors/slack.py),
[`devportal.py`](../../src/connectors/devportal.py). The fourth,
`city_map.py`, is much bigger and gets its own chapter.

## 3.1 The contract

```python
class BaseConnector(ABC):
    @abstractmethod
    def iter_documents(self) -> Iterator[Document]:
        """Yield normalized documents lazily, one at a time."""

    def load(self) -> list[Document]:
        """Materialize every document. Only safe for sources known to be small."""
        return list(self.iter_documents())
```

That is the entire base class. Four things to understand in it.

**`ABC` + `@abstractmethod`.** `ABC` means "abstract base class". Marking
`iter_documents` abstract means Python *refuses to instantiate* any subclass that
forgot to implement it — the error arrives when you construct the connector, not
when the pipeline is halfway through a run at 2am.

**`Iterator[Document]` and the word `yield`.** This is the important one.

A normal function that returns a list builds the whole list in memory first:

```python
def load_all():           # 200 GB source -> 200 GB in RAM -> your process dies
    docs = []
    for row in source:
        docs.append(make_document(row))
    return docs
```

A function containing `yield` is a **generator**. It produces one value, pauses,
and only continues when the caller asks for the next one:

```python
def iter_documents():     # 200 GB source -> one document in RAM at a time
    for row in source:
        yield make_document(row)
```

Memory use stops depending on how big the source is. That is why streaming is the
*primary* contract here rather than a convenience: a connector may sit in front
of a folder of PDFs and 900k-row CSVs, and nothing downstream should need the
whole source to fit in memory at once. Chapter 7 shows the pipeline consuming
this stream and never holding more than one batch.

**`load()` is a concession, not the interface.** It calls `list(...)` on the
generator, which materialises everything. It exists because tests and tiny
sources are more readable that way — `docs = JiraConnector().load()` in a test
beats a loop. The docstring explicitly limits it to "sources known to be small".
When you see `load()` in `tests/test_connectors.py`, that is why.

**There is a fifth, informal part of the contract.** `CityMapConnector` sets
`self.failures` — a list of `(filename, reason)` pairs for files it could not
read. The pipeline collects it in
[`pipeline.py:229-230`](../../src/ingestion/pipeline.py#L229-L230):

```python
for connector in connectors:
    stats.failures.extend(getattr(connector, "failures", []))
```

`getattr(x, "failures", [])` means "give me `x.failures`, or `[]` if it has
none". So a connector *may* report failures, and one that does not is not broken.
This is a deliberately loose coupling; the honest criticism is that it is
undocumented in the base class. If you wanted to tighten it, you would add
`failures: list[tuple[str, str]]` to a `BaseConnector.__init__`.

## 3.2 Jira — the fullest example

Read [`jira.py:17-44`](../../src/connectors/jira.py#L17-L44) alongside this.

```python
raw = json.loads(self.path.read_text(encoding="utf-8"))
for ticket in raw:
```

Today the "Jira API" is a sample JSON file. That is what the docstring means by
*"swap for Jira REST API later"* — the only thing that would change is these two
lines. Everything below them already works against real tickets. Notice the
constructor takes an optional `path`, defaulting to the sample: that is what lets
tests point it at a fixture without any mocking.

```python
comments = ticket.get("comments") or []
```

Why `.get("comments") or []` and not `.get("comments", [])`? Because the second
form only defends against a *missing* key. If the JSON contains
`"comments": null`, `.get("comments", [])` returns `None` and the next line
crashes. `or []` catches missing, null, and empty alike. This idiom is used
consistently throughout the connectors — now you know it is deliberate.

```python
body_parts = [
    ticket.get("description") or "",
    f"Status: {ticket.get('status')}",
    f"Labels: {', '.join(ticket.get('labels') or [])}",
    f"Assignee: {ticket.get('assignee')}",
]
if comment_text:
    body_parts.append("Comments:\n" + comment_text)
...
body="\n\n".join(p for p in body_parts if p),
```

Look at what this is doing: **turning structured fields into prose**. The status,
labels and assignee are not left as metadata only — they are written into the
body as readable lines. Why? Because the body is what gets embedded. A question
like *"which capability tickets are still open?"* can only match if the word
"Status" and the value "Open" are part of the embedded text. Structured data that
never enters the text is invisible to semantic search.

The `if p` in the join drops empty parts so you never get a blank gap in the middle.

```python
id=f"jira:{ticket['key']}",
```

Namespaced (ch. 2) and **stable** — derived from the ticket key, not from a
counter or a timestamp. Re-run tomorrow and CAP-101 gets the same id, which is
the precondition for change detection working at all.

Note `ticket['key']` uses square brackets, not `.get()`. That is a judgement: a
ticket without a key is *corrupt*, and crashing loudly is better than indexing a
document with a broken id. Compare with `.get("status")` — a missing status is
survivable. The bracket-vs-get choice quietly encodes what is required and what
is optional.

```python
url=ticket.get("url") or f"https://jira.example.com/browse/{ticket['key']}",
```

A citation must be clickable, so the URL is reconstructed when absent rather than
left empty.

```python
metadata={"key": ..., "status": ..., "labels": ...},
```

Some fields appear in both body and metadata. Not duplication for its own sake:
the body copy is for *semantic matching*, the metadata copy is for *filtering and
display* — an exact value you can compare, not a phrase buried in prose.
(Chapter 5 shows metadata being narrowed to scalars, which is where `labels` —
a list — gets dropped.)

```python
def _parse_dt(value): ...  # datetime.fromisoformat(value) if value else None
```

A leading underscore is Python's convention for "private to this module". The
comment notes that Python 3.11+ parses a trailing `Z` natively — the project
requires 3.11 in `pyproject.toml`, so no extra dependency is needed.

## 3.3 Slack — the shape of a document is a decision

[`slack.py:16-36`](../../src/connectors/slack.py#L16-L36)

```python
for thread in raw:
    messages = thread.get("messages") or []
    if not messages:
        continue
    body = "\n".join(f"{m['user']}: {m['text']}" for m in messages)
```

**One document per *thread*, not per message.** This is the real decision in this
file, and it is a retrieval decision disguised as a parsing choice.

A single Slack message — "yeah, same thing happened to me" — is meaningless
alone. Its meaning lives in the exchange: someone asked, someone answered,
someone confirmed. Embed each message separately and you get dozens of vectors
that mean almost nothing. Embed the thread as one unit and you get a vector for
*"registration fails when owner_team is missing, here is the fix"*, which is
exactly what a question will match.

The `f"{m['user']}: {m['text']}"` format keeps the speakers, so the LLM reading
this evidence later can tell a question from an answer.

```python
title = f"#{thread['channel']} — {root['text'][:80]}"
```

Threads have no title, so the first 80 characters of the opening message stand
in. Since titles are part of `to_text()` and therefore embedded, this matters:
the opening message is usually the question, which is usually the best one-line
summary of the thread.

```python
url=thread.get("url") or "",
```

Slack is the one place a URL may end up empty. Nothing crashes — but a citation
to this thread has no link. Worth knowing as a real (small) rough edge.

## 3.4 DevPortal — the simplest one

[`devportal.py:15-26`](../../src/connectors/devportal.py#L15-L26)

```python
for md_file in sorted(self.path.glob("*.md")):
```

`glob("*.md")` lists matching files; **`sorted()` is the interesting part**.
Filesystem order is not guaranteed to be stable across machines or runs. Sorting
makes ingestion deterministic: same input, same order, same output, every time.
That is what makes two runs comparable and bugs reproducible. It costs one word.

```python
title = _first_heading(text) or md_file.stem.replace("-", " ").title()
```

Use the document's own `# Heading` if it has one; otherwise derive a readable
title from the filename (`capability-lifecycle` becomes `Capability Lifecycle`).
`.stem` is the filename without its extension. Again: titles are embedded, so a
bad title is a retrieval cost, not a cosmetic one.

Markdown needs no extraction at all — it is already text with structure, and the
`#`/`##` headings it carries are exactly what the chunker splits on (ch. 5).
That is why this connector is 33 lines and the city map one is 211.

## 3.5 Adding a fifth source — the whole checklist

1. Add the value to `SourceType` in [`models/document.py`](../../src/models/document.py).
2. Write `connectors/confluence.py` with a class implementing `iter_documents()`.
3. Export it in [`connectors/__init__.py`](../../src/connectors/__init__.py).
4. Add it to `build_connectors()` in
   [`pipeline.py:54-55`](../../src/ingestion/pipeline.py#L54-L55).
5. **Add it to the router prompt** in
   [`nodes.py:32-36`](../../src/agent/nodes.py#L32-L36) — this is the step that
   gets forgotten. `VALID_SOURCES` is derived from the enum, so the source
   becomes *legal* automatically, but the LLM will never *choose* it unless the
   prompt describes it. The symptom is a source that is never routed to and never
   searched, with no error anywhere.

Five small edits, and no changes to chunking, embedding, indexing, retrieval or
the API. That is chapter 2's payoff, made concrete.

## Say this out loud

> "A connector's job is: read a source, and yield normalized `Document`s lazily.
> Lazily, because a source can be far bigger than memory. The interesting
> decisions are what counts as one document — a Jira ticket, a whole Slack
> thread — and which structured fields get written into the body, because only
> body text gets embedded and therefore only body text is searchable."

## Prove it to yourself

1. In a shell:
   `from src.connectors import SlackConnector; print(SlackConnector().load()[0].to_text())`.
   That string is literally what gets embedded. Read it and ask whether *you*
   could answer a question from it.
2. Prove laziness: put a `print("reading", ticket['key'])` inside the Jira loop,
   then run `it = JiraConnector().iter_documents()` — nothing prints until you
   call `next(it)`. That is the generator pausing.
3. Break the contract on purpose: make a class that inherits `BaseConnector`
   without defining `iter_documents`, and try to instantiate it. Read the error.
