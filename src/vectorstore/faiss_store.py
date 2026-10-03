"""Optional exact FAISS acceleration with SQLite as publication authority.

Install the `faiss-cpu` package before selecting this backend. The FAISS index is
derived lazily from a consistent snapshot of active SQLite rows, so another
worker's committed changes become visible without unsafe pickle snapshots.
"""
import json
import threading

from .sqlite import SQLiteVectorStore


class FAISSVectorStore(SQLiteVectorStore):
    def __init__(self, path="data/runtime/documents.sqlite"):
        try:
            import faiss
            import numpy
        except ImportError as exc:
            raise RuntimeError("The FAISS backend requires the optional faiss-cpu package") from exc
        self._faiss, self._np = faiss, numpy
        self._faiss_lock = threading.RLock()
        self._snapshot_ids = None
        self._index = None
        super().__init__(path)

    def _cosine_scores(self, vector, rows):
        identity = tuple(row["chunk_id"] for row in rows)
        with self._faiss_lock:
            if identity != self._snapshot_ids:
                matrix = self._np.asarray([json.loads(row["vector"]) for row in rows], dtype="float32")
                self._faiss.normalize_L2(matrix)
                index = self._faiss.IndexFlatIP(len(vector))
                index.add(matrix)
                self._index, self._snapshot_ids = index, identity
            query = self._np.asarray([vector], dtype="float32")
            self._faiss.normalize_L2(query)
            similarities, indexes = self._index.search(query, len(rows))
            scores = [0.] * len(rows)
            for index, score in zip(indexes[0], similarities[0]):
                if index >= 0:
                    scores[int(index)] = float(score)
            return scores


FaissVectorStore = FAISSVectorStore
