from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RetrievalPlan:
    needs_search: bool
    search_query: str
    transaction_ids: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    source: str  # file absolute filepath to .json document
    title: str
    transaction_id: str
    raw_text: str
