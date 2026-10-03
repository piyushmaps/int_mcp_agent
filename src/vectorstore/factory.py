from pathlib import Path

from .sqlite import SQLiteVectorStore


def get_vector_store(settings=None, *, path=None, backend=None, **options):
    """Select the transactional local backend; optional adapters are explicit."""
    location = path or (Path(settings.data_dir) / "documents.sqlite" if settings is not None else "data/runtime/documents.sqlite")
    backend = backend or (settings.vector_backend if settings is not None else "sqlite")
    if backend == "faiss":
        from .faiss_store import FAISSVectorStore
        return FAISSVectorStore(location)
    if backend == "opensearch":
        from .opensearch_store import OpenSearchVectorStore
        if settings is not None:
            options = {"endpoint": settings.opensearch_endpoint, "index_name": settings.opensearch_index,
                       "username": settings.opensearch_username,
                       "password": settings.opensearch_password.get_secret_value(),
                       "approved_remote_processing": settings.approved_remote_indexing, **options}
        return OpenSearchVectorStore(location, **options)
    if backend != "sqlite":
        raise ValueError("Supported vector stores are sqlite, faiss and opensearch")
    return SQLiteVectorStore(location)
