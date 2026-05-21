from dataclasses import dataclass
from typing import Any
from enum import Enum

class LoadDataStrategy(Enum):
    AddNew = 1
    ReloadAll = 2
    OldDataOnly = 3


@dataclass(frozen=True)
class DocumentMetadata:
    offer_id: str
    seq_num: int    # like a chunk index
    source: str
    source_type: str
    title: str


@dataclass(frozen=True)
class RetrievalPlan:
    needs_search: bool
    search_query: str
    offer_ids: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    filepath: str  # file absolute filepath to .json document
    source_url: str
    title: str
    offer_id: str
    raw_text: str
