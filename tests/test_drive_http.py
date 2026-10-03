import httpx
import pytest

from src.connectors.drive import GoogleDriveConnector
from src.connectors.drive_http import GoogleDriveHTTPTransport
from src.contracts import Document
from src.ingestion.pipeline import IngestionPipeline
from src.vectorstore.sqlite import SQLiteVectorStore


def test_drive_http_reads_only_current_company_domain_items():
    requests = []
    def serve(request):
        requests.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.headers["authorization"] == "Bearer test-only-token"
        if request.url.path.endswith("/permissions"):
            return httpx.Response(200, json={"permissions": [{"type": "domain", "domain": "example.com", "role": "reader"}]})
        if request.url.path.endswith("/export"):
            return httpx.Response(200, text="Payments rollout requires a verified release gate.")
        return httpx.Response(200, json={"files": [
            {"id": "approved", "name": "Guide", "version": "2", "mimeType": "application/vnd.google-apps.document",
             "webViewLink": "https://docs.google.com/document/d/approved/edit"},
            {"id": "unapproved", "name": "Private", "version": "1", "mimeType": "text/plain"},
        ]})
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        transport = GoogleDriveHTTPTransport("test-only-token", company_domain="example.com", approved_folders={"folder"},
                                             approved_items={"approved"}, client=client)
        connector = GoogleDriveConnector(transport, "folder")
        documents = connector.load()
        assert connector.complete and len(documents) == 2
        assert documents[0].company_wide and "release gate" in documents[0].body
        assert not documents[1].company_wide and not documents[1].body
        assert sum(r.url.path.endswith("/permissions") for r in requests) == 2
        with pytest.raises(ValueError):
            list(transport.list_items("another-folder"))
        with pytest.raises(ValueError):
            transport.read_bytes({"id": "unapproved"})


def test_drive_http_incomplete_listing_cannot_mark_scope_complete():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"incompleteSearch": True}))) as client:
        transport = GoogleDriveHTTPTransport("test", company_domain="example.com", approved_folders={"folder"},
                                             approved_items=set(), client=client)
        connector = GoogleDriveConnector(transport, "folder")
        assert connector.load() == []
        assert not connector.complete and connector.failures


@pytest.mark.parametrize("identity", ["password=secrets-in-filename", "employee@example.com"])
def test_source_identity_cannot_bypass_screening(identity):
    store = SQLiteVectorStore(":memory:")
    try:
        stats = IngestionPipeline(store).ingest([Document(id=identity, source="local", title="Safe title", body="Payments rollout guidance.")])
        assert stats.documents_quarantined == 1
        assert store.count() == 0
    finally:
        store.close()

