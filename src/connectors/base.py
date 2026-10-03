from abc import ABC, abstractmethod
from typing import Iterator

from src.contracts import Document


class BaseConnector(ABC):
    source = "documents"

    def __init__(self):
        self.failures: list[tuple[str, str]] = []
        self.complete = False

    @property
    def scope(self) -> str:
        raise NotImplementedError("Connectors must identify the exact enumeration scope")

    @abstractmethod
    def iter_documents(self) -> Iterator[Document]:
        """Yield records; mark complete only after a fully successful enumeration."""

    def load(self) -> list[Document]:
        return list(self.iter_documents())
