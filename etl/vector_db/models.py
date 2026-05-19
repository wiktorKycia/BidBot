from dataclasses import dataclass
from typing import Any
from enum import Enum

class SourceType(Enum):
    JSON = 1
    ATTACHMENT = 2


@dataclass(frozen=True)
class DocumentMetadata:
    offer_id: str
    seq_num: int    # like a chunk index
    source: str
    source_type: SourceType
    title: str


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

@dataclass(frozen=True)
class OfferRecord:
    pass


@dataclass(frozen=True)
class AttachmentRecord:
    pass

@dataclass(frozen=True)
class ChunkRecord:
    pass
