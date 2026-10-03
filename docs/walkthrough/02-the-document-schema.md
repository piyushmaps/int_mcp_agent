# 2 — The Document schema: the keystone decision

**File:** [`src/models/document.py`](../../src/models/document.py) — 33 lines. The
smallest file in the project and the most important one.

## The problem it solves

You have four sources. Jira gives you tickets with statuses and comments. Slack
gives you threads with messages and users. DevPortal gives you Markdown files.
The city map gives you a JSON graph, CSVs, PDFs, HTML pages and an email.

Downstream you need to: chunk text, embed it, index it, search it, and cite it.

If every downstream stage had to know about every source, you get a multiplying
mess: the chunker needs a Jira branch and a Slack branch and a PDF branch; the
citation code needs to know that Jira has a `key` but Slack has a `thread_ts`.
Add a fifth source and you edit six files.

So instead: **every source converts to one common shape at the boundary, and
nothing downstream ever knows where a document came from** — except as a plain
string tag it can filter on.

That shape is `Document`.

## Reading the file

```python
class SourceType(str, Enum):
    JIRA = "jira"
    SLACK = "slack"
    DEVPORTAL = "devportal"
    CITY_MAP = "city_map"
```

An `Enum` is a fixed set of named values. Writing `SourceType.JIRA` instead of
the string `"jira"` means a typo (`"jria"`) is caught immediately instead of
silently matching nothing at search time.

Note `(str, Enum)` — it inherits from `str` as well. That means a `SourceType`
**is** a string: it can be compared to `"jira"`, serialised into JSON, and used
as a dict key without conversion. That is why you will see both `doc.source`
(the enum) and `doc.source.value` (the plain string) around the codebase, and
why either works.

This enum is also the single source of truth for routing. In
[`src/agent/nodes.py:16`](../../src/agent/nodes.py#L16):

```python
VALID_SOURCES = {s.value for s in SourceType}
```

The router's output is filtered against that set. Add a source to the enum and
the router can select it; forget to, and it is silently dropped. (Chapter 3 has
the full checklist for adding a source.)

```python
class Document(BaseModel):
```

`BaseModel` is from **pydantic**. It gives you three things for free:

- **Validation.** Construct a `Document` without an `id`, or with `source="jra"`,
  and it raises immediately — at the connector, where the bug is, not three
  stages later in the indexer.
- **Coercion.** A field typed `datetime` accepts a value and converts it.
- **Serialisation.** `.model_dump()` / `.model_dump_json()` for free.

Now the fields, and *why each one exists*:

```python
    id: str
```
Unique across the whole system, not just within its source. Connectors namespace
it: `jira:CAP-101`, `slack:1712345678.001`, `city_map:payments`. Without the
prefix, a Jira ticket and a Slack thread could collide on the same id. This id
is also what the manifest hashes against (ch. 7) and what chunk ids are built
from (ch. 5) — so it has to be **stable across runs**. If an id changed every
run, nothing would ever be recognised as "unchanged".

```python
    source: SourceType
```
The tag that survives all the way to query time. It is copied into every chunk's
metadata and is what "search only DevPortal and Slack" filters on.

```python
    title: str
    body: str
    url: str
```
`title` and `body` are the content. `url` is what makes a **citation** possible —
the answer can link the reader back to the ticket, thread, or doc. Notice that
the sample connectors invent plausible URLs (`https://jira.example.com/browse/...`)
rather than leaving them blank: the field is load-bearing, so it always gets a value.

```python
    author: str | None = None
    updated_at: datetime | None = None
```
Optional, because Slack and DevPortal don't always have them. `str | None` is
modern Python for "a string or nothing" (same as `Optional[str]`).

```python
    metadata: dict[str, Any] = Field(default_factory=dict)
```
The escape hatch: anything source-specific that doesn't fit the common shape —
Jira's status and labels, Slack's channel, a PDF's page range.

**Why `Field(default_factory=dict)` and not `= {}`?** This is a classic Python
trap. A default value is created *once*, when the class is defined, and shared by
every instance. `= {}` would give every Document the *same* dictionary — writing
to one document's metadata would change all of them. `default_factory=dict` says
"call `dict()` fresh for each new instance." Remember this one; it bites people.

```python
    def to_text(self) -> str:
        header = f"[{self.source.value.upper()}] {self.title}"
        return f"{header}\n\n{self.body}".strip()
```

This is the **one text representation** used everywhere else: hashing for change
detection, chunking, embedding, keyword search. Everything agrees on what "the
content of this document" means because they all call this method.

The header is not decoration. The text that gets embedded includes the source
tag and the title, so a search for "capability lifecycle" matches a document
whose *title* is "Capability Lifecycle" even if the body words differ. Titles
are dense with meaning; leaving them out of the embedding throws that away.

## The pattern, named

This is called **normalization at the boundary** — sometimes an anti-corruption
layer. The messy, source-specific world is confined to `src/connectors/`. The
moment data crosses out of that folder it is a `Document`, and the other ~1,500
lines of the project only ever handle `Document`s and their chunks.

Cost of adding a fifth source: one new file in `connectors/`, one enum value,
one line in `build_connectors()`, one line in the router prompt. Nothing else in
the codebase changes. That is the payoff, and it is the single strongest thing
about this project's structure.

## Say this out loud

> "Every source normalizes to one `Document` shape at the connector boundary, so
> chunking, embedding, indexing, retrieval and citation are written once instead
> of once per source. `to_text()` is the agreed definition of a document's
> content, and the source tag survives into chunk metadata so retrieval can
> filter by it later."

## Prove it to yourself

Open a Python shell and build one by hand:

```python
from src.models.document import Document, SourceType
d = Document(id="test:1", source=SourceType.JIRA, title="Hello", body="World", url="u")
print(d.to_text())
print(d.model_dump())
Document(id="x", source="not-a-source", title="t", body="b", url="u")  # watch it raise
```

Then find, in [`src/ingestion/chunking.py:42-47`](../../src/ingestion/chunking.py#L42-L47),
the four `Document` fields that get copied onto every single chunk. Ask yourself
why *those* four and not the others. (Answer: they are exactly what a citation
needs, plus the filter tag.)
