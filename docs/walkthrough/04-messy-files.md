# 4 — Messy files: extractors and the city map

**Files:** [`src/ingestion/extractors.py`](../../src/ingestion/extractors.py) (298
lines) and [`src/connectors/city_map.py`](../../src/connectors/city_map.py) (211
lines). This is the messiest part of the project, because reality is messy. It is
also where the single most valuable engineering decision in the repo lives —
section 4.4.

The city map is not really one source. It is a folder containing a JSON graph, a
handful of CSVs (5 rows to 900k rows), HTML pages, PDFs including entire
textbooks, an email, and a macOS junk file. Yet it produces the same `Document`s
as Jira does.

## 4.1 The dispatch table

At the bottom of `extractors.py`:

```python
EXTRACTORS: dict[str, Callable[[Path], Extracted]] = {
    ".pdf": extract_pdf,
    ".html": extract_html,
    ".htm": extract_html,
    ".csv": extract_sheet,
    ".eml": extract_eml,
}

def register_extractor(suffix, extractor) -> None:
    EXTRACTORS[suffix.lower()] = extractor

def is_supported(path) -> bool:
    return path.suffix.lower() in EXTRACTORS

def extract_file(path) -> Extracted | None:
    extractor = EXTRACTORS.get(path.suffix.lower())
    return extractor(path) if extractor is not None else None
```

A dictionary from file extension to a function. `Callable[[Path], Extracted]` is
a type saying "a function taking a Path and returning an `Extracted`".

Why a table rather than a chain of `if path.suffix == ".pdf": ... elif ...`?
Because adding `.docx` support becomes *one function plus one line*, with no
existing code edited. Connectors, chunking and indexing never learn a new
format exists — they only ever see `Document`s. This is the open/closed
principle, and `register_extractor` makes it usable from outside the module.

`extract_file` returning `None` for unknown suffixes rather than raising is a
deliberate choice too: an unsupported file is a normal event in a shared folder,
not an error.

Every extractor returns the same little box:

```python
@dataclass
class Extracted:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

Text for embedding, plus whatever format-specific facts the connector should keep
for citations (page numbers, row counts, the HTML title). Same
`default_factory=dict` rule as in chapter 2.

## 4.2 The four extractors

**PDF** ([`extract_pdf`](../../src/ingestion/extractors.py#L47-L58)) — pulls text
page by page and prefixes each with `[page N]`. Those markers survive into the
chunk text, so when the LLM cites a chunk the page is visible. Pages that extract
to nothing (scans, images) are skipped rather than emitting blanks. This is
*digital* PDF text extraction, not OCR: a scanned document produces nothing here,
which is a real limitation worth being able to name.

**HTML** ([`extract_html`](../../src/ingestion/extractors.py#L61-L92)) — two
passes with BeautifulSoup:

```python
for tag in soup(_HTML_NOISE_TAGS):     # nav, footer, script, style, header, aside, button
    tag.decompose()
for banner in soup.select(".cookie-banner, .ad, .promo"):
    banner.decompose()
```

`decompose()` deletes an element from the tree. Without this, every page
contributes its navigation menu and cookie banner to the index, and those
identical fragments compete with real content in search results.

Then the second pass rebuilds the page as text — but converts `<h1>/<h2>/<h3>`
into `#`, `##`, `###` and `<li>` into `- `. That is not cosmetic:
[`chunking.py:32`](../../src/ingestion/chunking.py#L32) splits preferentially on
`"\n## "` and `"\n# "`. The HTML extractor is deliberately emitting the markers
the chunker looks for, so chunks land on section boundaries. Two files, one
agreement — worth noticing, and worth preserving if you edit either.

**Sheet** ([`extract_sheet`](../../src/ingestion/extractors.py#L95-L109)) —
renders each row as `col: value | col: value`. A bare CSV line
`Payments,money-movement,alex.chen` means nothing on its own; with headers
attached it reads as a sentence a search can match. Also note line 31:

```python
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
```

The standard library caps a single CSV field at 128 KB. A dataset with a full
news article in one cell blows through that. The `min(...)` guard is there
because `sys.maxsize` is larger than a C long on Windows and would itself raise.
Both halves of that line are scar tissue from a real crash.

**EML** ([`extract_eml`](../../src/ingestion/extractors.py#L112-L128)) — the
"long tail" example, using the standard library's email parser.
`get_body(preferencelist=("plain", "html"))` prefers the plain-text alternative.
The From/To/Subject/Date header is prepended into the text so a question like
"what did the ledger-core deprecation announcement say?" can match on the subject.

## 4.3 Streaming a giant PDF

[`iter_pdf_parts`](../../src/ingestion/extractors.py#L244-L277) exists because
`extract_pdf` is fine for a 5-page architecture doc and terrible for a
1,000-page calculus textbook.

```python
reader = PdfReader(str(path))
total = len(reader.pages)
for start in range(0, total, pages_per_part):
    end = min(start + pages_per_part, total)
```

It walks the PDF in windows of `pdf_pages_per_document` (default 50, from
config), yielding one `Extracted` per window. Two wins:

- **Memory** is bounded by the window, not the book. pypdf reads pages lazily, so
  only the current window is resident.
- **Citations get specific.** The connector turns each part into its own
  Document titled `... (pages 101-150)` with a URL ending `#page=101`. One
  document for the whole book would produce thousands of chunks all citing "the
  book", which is useless to a reader.

```python
try:
    text = (reader.pages[number].extract_text() or "").strip()
except Exception as exc:
    logger.debug(...)
    continue
```

One corrupt page is skipped; the other 49 pages of that part still make it in.
Note the granularity of the error handling here — per page inside the extractor,
per file inside the connector (4.5), and fatal at the embedding API (ch. 7). The
rule being applied is: *lose the smallest unit you can, but never silently lose
correctness.*

## 4.4 The 900k-row problem — the best decision in the repo

Here is what happened. A folder of real exports arrived: a 900k-row crime table,
a 60k-row insurance table, three calculus textbooks. The obvious reading of
"index everything" produced **1,149,072 documents** — several GB of vectors,
hours of embedding, and about $5 of API spend, for a corpus where a typical row is:

```
4.5, 2.3, 1.3, 0.3, Setosa
```

Those rows all embed to nearly the same point. Semantic search over a million of
them returns noise, at real cost. Nobody will ever ask a question whose answer is
row 448,201.

The fix is to treat a sheet by **what it is**, not by its extension. Three cases,
implemented in [`_iter_sheet`](../../src/connectors/city_map.py#L163-L195):

| Sheet | Treatment | Why |
|---|---|---|
| Small reference table (≤ 500 rows) | one Document per row | "who owns Payments" should match exactly one row |
| Large data table | **one profile Document** | schema + row count + per-column stats + samples answers "what is in this dataset?" without a million vectors |
| Large table with a prose column | profile **plus** rows, capped at 2,000 | news articles and ticket bodies genuinely are worth retrieving individually |

Result: **1,149,072 documents became 4,577**, and roughly $5 and several hours
became $0.13 and minutes — with no loss of anything a question could actually
reach. That is the number to remember about this project.

### How a sheet is profiled

[`profile_sheet`](../../src/ingestion/extractors.py#L164-L192):

```python
with path.open(encoding="utf-8", newline="", errors="replace") as f:
    reader = csv.DictReader(f)
    columns = [c for c in (reader.fieldnames or []) if c]
    for row in reader:
        row_count += 1
        if len(sample) < sample_rows:
            sample.append(row)
```

One streaming pass. Every row is *counted*, but only the first 500 are *kept*.
A 200 MB file profiles in the same memory as a 2 KB one. `errors="replace"`
means a stray non-UTF-8 byte becomes a replacement character instead of killing
the run — the right trade for a file you did not create.

Then the heuristic that decides everything:

```python
TEXT_COLUMN_MIN_CHARS = 200

text_column = max(
    (c for c in columns
     if by_column[c] and statistics.fmean(len(v) for v in by_column[c]) >= TEXT_COLUMN_MIN_CHARS),
    key=lambda c: statistics.fmean(len(v) for v in by_column[c]),
    default=None,
)
```

Read it inside out: for each column, take the mean *length* of its sampled
values; keep the columns averaging 200+ characters; of those, take the longest.
`default=None` is what `max()` returns when nothing qualifies — without it,
`max()` on an empty sequence raises.

The whole insight is that single line: **a column whose cells average 200+
characters holds prose; anything shorter is a category, a code, or a number.**
`is_text_bearing` is just `text_column is not None`, and that boolean decides
whether 2,000 rows get embedded or none do.

It is a heuristic, and you should be able to say how it fails: a column of
150-character descriptions is missed; a column of long URLs or base64 blobs is
falsely called prose. Both are acceptable — the cost is a few thousand mediocre
chunks, not a million.

[`render_profile`](../../src/ingestion/extractors.py#L195-L223) then writes the
profile as Markdown: dataset name, row count, column list, one summary line per
column, three sample rows. `_summarize` tries `float()` on the values and reports
min/max/mean if they are numbers, or the four most common values if they are
categories. This one document answers "what data do we have about crime?" —
which is the question people actually ask about a big table.

## 4.5 The city map connector

[`iter_documents`](../../src/connectors/city_map.py#L53-L62) is the whole shape:

```python
yield from self._iter_graph(self.path / GRAPH_FILE)
for file in sorted(self.path.rglob("*")):
    if not self._should_read(file):
        continue
    try:
        yield from self._iter_file(file)
    except Exception as exc:
        self.failures.append((file.name, f"{type(exc).__name__}: {exc}"))
        logger.warning("skipping %s — %s: %s", file.name, type(exc).__name__, exc)
```

`yield from` forwards every value from an inner generator, keeping the whole
chain lazy. `rglob("*")` walks subfolders too; `sorted()` again for determinism.

**The try/except is the production-grade part.** Nine hours into ingesting a
large folder, one corrupt PDF should cost that file, not the job. The failure is
recorded with the exception type and message, printed in the run summary, and the
stream continues. Catching bare `Exception` is normally a smell — here it is the
point, and the `# noqa: BLE001` comment tells the linter it is intentional.

[`_should_read`](../../src/connectors/city_map.py#L64-L70) has a detail worth
seeing:

```python
if file.name.startswith("._"):
    return False
```

macOS writes `._Insurance claims data.csv` sidecar files. They carry a real `.csv`
extension and binary garbage. There is one in `data/sample/city_map/`. Without
this line, ingestion either crashes or indexes noise. This is what "handles real
data" looks like in practice — an unglamorous two-line guard.

[`_iter_graph`](../../src/connectors/city_map.py#L74-L109) turns the capability
graph into documents. Nodes become documents; edges are pre-joined into each
node's body as a dependency list:

```python
deps: dict[str, list[str]] = defaultdict(list)
for edge in raw.get("edges") or []:
    deps[edge["from"]].append(f"{edge['type']} → {edge['to']}")
```

`defaultdict(list)` auto-creates an empty list for a new key, so no
"if key not in dict" dance. The important idea: a graph edge is not searchable on
its own, so relationships are **flattened into the text of the node they belong
to**. That is how "what does Payments depend on?" becomes answerable by a plain
text search.

Also note `if not path.is_file(): return` — a missing `capabilities.json` is not
fatal, because the folder's PDFs and sheets should still ingest.

[`_key`](../../src/connectors/city_map.py#L156-L159) builds the id fragment from
the path *relative to the source root*, so `2023/report.pdf` and `2024/report.pdf`
do not collide. Same "ids must be stable and unique" rule from chapter 2, applied
to files instead of tickets.

## Say this out loud

> "Extractors are a suffix-to-function dispatch table, so a new format is one
> function and one registration. Big PDFs stream as page ranges so memory is
> bounded and citations point at real pages. And sheets are indexed by what they
> are: small reference tables row by row, big tables as a single profile
> document. That last rule took the corpus from 1.15 million documents to 4,577
> without losing any answerable content."

## Prove it to yourself

```python
from pathlib import Path
from src.ingestion.extractors import profile_sheet, render_profile
p = Path("data/sample/city_map/Crime.csv")
prof = profile_sheet(p)
print(prof.row_count, prof.text_column, prof.is_text_bearing)
print(render_profile(p, prof).text)
```

Then run it against `capability-owners.csv` and watch it take the other branch.
Finally, count what the connector actually produces:

```python
from collections import Counter
from src.connectors import CityMapConnector
c = CityMapConnector()
print(Counter(d.metadata.get("format") for d in c.iter_documents()))
print(c.failures)
```
