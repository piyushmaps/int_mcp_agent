# Ingestion and retrieval guide

The runnable application uses SQLite for durable sanitized content, vectors and
active-version metadata. Signed token hashing is the offline default embedding
model. It is deterministic lexical feature hashing, **not a learned semantic
model**. BM25 and cosine rankings are combined with reciprocal rank fusion;
the exposed evidence `score` remains cosine similarity.

The earlier walkthroughs describe a historical FAISS/OpenSearch implementation.
This implementation keeps optional adapters for both while choosing SQLite as
the transactional small-corpus baseline. This is an explicit implementation
decision, not a claim that SQLite is a distributed vector database.

## Run the synthetic demo

From the project directory, with dependencies installed:

```powershell
python -m src.ingestion.pipeline --demo
python -m src.ingestion.pipeline --demo
```

The second run skips unchanged records. The four demo records come from
`src.bootstrap.demo_documents()` and use the same identities as the API bootstrap.
Revoked fixtures are not resurrected. These are invented examples, not company
facts. Project architecture/interview documents are never automatically ingested.

For an approved local directory:

```powershell
python -m src.ingestion.pipeline --root data/approved-documents --no-prune
python -m src.ingestion.pipeline --root data/approved-documents --full-refresh
```

`--index` selects the SQLite file; the default is
`data/runtime/documents.sqlite`. The CLI's default embedding is always offline
hashing. If the API is configured for OpenAI embeddings, use the Python API below
with the matching embedding adapter and a separate index during migration.
`--full-refresh` reprocesses matching documents; it does not migrate an existing
index to a different embedding configuration. `--no-prune` disables disappearance
inference. With pruning enabled, only records in a fully enumerated connector
scope can be removed. Any extraction failure suppresses pruning for that scope.

The local connector accepts Markdown/text, HTML, digital PDF, Word `.docx`, CSV
and JSON. It retains PDF page numbers, Markdown sections, Word headings/table
rows and CSV column names. Input is bounded to 20 MiB and extracted text to two
million characters. PDFs are limited to 500 pages and CSVs to 10,000 rows.
Unreadable scans require an independently approved OCR integration. Legacy
`.doc` files are not supported. Oversized documents fail visibly instead of
silently indexing a prefix.

Uploaded files use a parser subprocess with a deadline and bounded result file.
POSIX workers additionally receive CPU/memory/file-size limits. Windows provides
process timeout and size guards here. This is fault isolation; an OS/container
security sandbox is still required for a public upload service.

## Python API and version lifecycle

```python
from src.connectors import LocalDirectoryConnector
from src.ingestion.pipeline import IngestionPipeline
from src.retrieval import SourceRetriever
from src.vectorstore import SQLiteVectorStore

store = SQLiteVectorStore("data/runtime/documents.sqlite")
pipeline = IngestionPipeline(store)
connector = LocalDirectoryConnector("data/approved-documents", source="devportal",
                                    company_wide=True)
stats = pipeline.ingest_connectors([connector], prune=True)
print(stats.render())
evidence = SourceRetriever(store).retrieve("Payments rollout approval")
store.close()
```

`company_wide` is connector configuration derived from trusted access metadata;
it is never accepted from document front matter. Only explicitly approved
company-wide sources belong in this application. Local citations use the API's
sanitized content viewer, not a local disk path or invented source URL.

The pipeline screens body, title, extraction blocks and metadata before
embedding. It tracks revision, processing versions, eligibility metadata,
locations and the embedding fingerprint. A content-only hash cannot override
permission changes. Embedding output count, dimensions and finite values are
validated. A failed embedding leaves the previous eligible revision active.

SQLite publishes a complete version, its chunks and the active pointer in one
transaction. Retrieval joins the active pointer and checks it again before
returning evidence. `store.is_current(evidence)` supports the answer release
gate. A verified restriction calls `store.revoke(source_id, revision=...)`
before cleanup. Revoked source text is removed from active SQLite tables.

Numeric source revisions cannot move backwards. A blocked revision cannot be
restored by replaying the same revision; it requires verified newer numeric
metadata. Opaque revisions on blocked records require a deliberate identity or
administrative recovery policy; the implementation does not guess their order.
SQLite backups/WAL and external provider retention need a separately managed
retention/deletion policy; deleting a row is not secure media erasure.

## Approved web pages and Google Drive

```python
from src.connectors import WebConnector

web = WebConnector(
    ["https://docs.your-company.example/release"],
    allowed_hosts={"docs.your-company.example"},
    company_wide=True,
)
stats = pipeline.ingest_connectors([web], prune=False)
```

Replace the example with a real approved source. The web reader only fetches
allowlisted public HTTPS hosts. It pins validated DNS addresses for the
connection, verifies TLS hostname/certificates, checks redirects, limits bodies
and time, and strips navigation/scripts from HTML. It is a finite page reader,
not an authenticated crawler. Internal/private IPs, query-string credentials,
compressed responses and non-text formats are rejected. DNS resolution uses the
OS resolver; its own timeout behavior remains an operating-system concern.

Drive supports an injected transport and a bounded read-only Drive v3 HTTP implementation:

```python
from src.connectors import GoogleDriveConnector
from src.connectors.drive_http import GoogleDriveHTTPTransport
import os

transport = GoogleDriveHTTPTransport(
    os.environ["DRIVE_ACCESS_TOKEN"], company_domain="your-company.example",
    approved_folders={"approved-folder-id"}, approved_items={"approved-file-id"},
)
drive = GoogleDriveConnector(transport, folder_id="approved-folder-id")
stats = pipeline.ingest_connectors([drive], prune=False)
transport.close()
```

Required metadata: `id`, `version`, `name`, `mimeType`, `webViewLink`, and a
trusted Boolean `company_wide`. Optional metadata includes `modifiedTime`,
`permission_version` and `trashed`. Restricted items are yielded without
downloading their content so a previous indexed version can be blocked.
Missing eligibility is denied. A partial listing or transport failure prevents
pruning. This adapter contract is tested locally; no Google account was connected
and no production permission-change feed is configured.

The HTTP transport limits folder and permission pages, rejects incomplete listings,
requires both an explicit item allowlist and current company-domain permission,
and checks permission again before reading bytes. Downloads and native-document
exports are bounded, redirects are disabled, and the OAuth token only goes to the
fixed Google API origin. Token acquisition/refresh is the caller's responsibility.
The implementation follows Google's [file-list API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
and [export API](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export).

## Durable source jobs

```python
from src.ingestion.jobs import IngestionJobQueue

queue = IngestionJobQueue(store)
queue.enqueue("drive:item-id", "42", scope="drive:folder:approved-folder-id",
              action="update", cursor="new-provider-cursor")
# Fetch current trusted metadata/content, not the event's untrusted payload.
queue.work_once(fetch_current_document, pipeline)
```

Job insertion and source cursor advancement share a transaction. Duplicate
source/revision/action events share an identity. Workers claim expiring leases;
lease tokens fence completion after a crash/reclaim. Failed work retries with a
delay and stops after four attempts. Only stable references and safe error
classes are stored in job records. `fetch_current_document` returns a current
`Document`, or `None` only for a verified deletion. Workers preserve prior
versions when new content fails preparation. Run source polling/change feeds
and worker scheduling under your process supervisor; there is no broker or
always-on production daemon bundled here.

## Optional embedding and search adapters

Remote embedding requires both credentials and an explicit processing approval:

```python
import os
from src.ingestion.embeddings import OpenAIEmbeddingAdapter

embeddings = OpenAIEmbeddingAdapter(
    api_key=os.environ["OPENAI_API_KEY"], approved_processing=True,
    model="text-embedding-3-small", dimensions=1536,
)
pipeline = IngestionPipeline(store, embeddings)
retriever = SourceRetriever(store, embeddings)
```

Use exactly the same adapter configuration for ingestion and questions. The
adapter rejects unscreened inputs, bounds each input conservatively by UTF-8
bytes, limits request sizes and has network timeouts/retries. Changing model or
dimension requires a new index and a coordinated reader cutover. An API key
alone never authorizes sending source content to a remote processor.

FAISS requires `pip install faiss-cpu`. Select it with:

```python
from src.vectorstore.factory import get_vector_store
store = get_vector_store(path="data/runtime/documents.sqlite", backend="faiss")
```

FAISS performs exact normalized inner products. SQLite remains durable and
authoritative; the in-memory FAISS snapshot is rebuilt when active chunk IDs
change. Keyword ranking and serving checks are shared with the local backend.
There is no unsafe pickle load or independent stale permission snapshot.

OpenSearch requires `pip install opensearch-py`, an approved cluster with its
k-NN plugin, and a single coordinated ingestion writer:

```python
store = get_vector_store(
    path="data/runtime/remote-authority.sqlite", backend="opensearch",
    endpoint=os.environ["OPENSEARCH_URL"],
    username=os.environ["OPENSEARCH_USERNAME"],
    password=os.environ["OPENSEARCH_PASSWORD"],
    index_name="engineering-atlas-v1", approved_remote_processing=True,
    # ca_certs="/path/to/company-ca-bundle.pem",
)
```

TLS certificate and hostname verification are enabled. The index mapping locks
the embedding configuration. Candidate batches are written and visibility is
checked before the local active pointer moves. Querying combines an exact
filtered cosine scoring script and BM25, then validates remote candidate IDs
against current local metadata. Remote text, URLs and scores never replace the
approved canonical evidence. This exact-search reference adapter does not claim
HNSW scale or a shared distributed authorization service. It follows the official
[OpenSearch scoring-script](https://docs.opensearch.org/latest/vector-search/vector-search-techniques/knn-score-script/)
and [Python-client](https://docs.opensearch.org/latest/clients/python-low-level/)
APIs, with certificate/hostname checks enabled.

The remote adapter is protocol-tested with a fake client, **not tested against
a live cluster**. Keep one writer and shared durable authority. Remote snapshots,
cluster access controls, encryption, service capacity, backups and availability
need deployment-specific verification. Stale remote versions can consume the
bounded candidate window and reduce recall; they never pass the active-version
gate. Retired/failed-version cleanup is durable and retriable, but intentionally
runs in a maintenance window with ingestion paused:

```python
store.cleanup_retired(ingestion_paused=True)
```

A crash between external writes and local cleanup bookkeeping can leave orphan
inactive remote records. Reconcile these during a maintenance window or rebuild
the derived remote index from approved local content. Do not describe this
cross-store reference integration as exactly-once or production-ready.

## Verification

```powershell
python -m pytest tests/test_ingestion.py -q
```

Tests cover unchanged skips, persistence, relevance, identical-text restrictions,
older revisions, failure preservation, shorter updates, screening before
embedding, no pruning after partial enumeration, bounded chunks, locations,
concurrent revocation, parser isolation, durable cursor/lease recovery and
remote provenance validation. The FAISS parity test skips unless its optional
package is installed. No remote credentials or enterprise datasets are needed.
