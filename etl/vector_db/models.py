from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RetrievalPlan:
    needs_search: bool
    search_query: str
    offer_ids: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    source: str  # file absolute filepath to .json document
    title: str
    offer_id: str
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
