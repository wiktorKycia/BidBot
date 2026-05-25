from dataclasses import dataclass
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class LoadDataStrategy(Enum):
    AddNew = 1
    ReloadAll = 2
    OldDataOnly = 3


@dataclass(frozen=True)
class DocumentMetadata:
    offer_id: str
    seq_num: int  # like a chunk index
    source: str
    source_type: str
    title: str


class RetrievalPlan(BaseModel):
    needs_search: bool = Field(description="Whether retrieval is needed")
    search_query: str = Field(description="A focused search query")
    offer_ids: list[str] = Field(description="Any offer IDs")
    top_k: int = Field(description="The desired top_k documents")


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    filepath: str  # file absolute filepath to .json document
    source_url: str
    title: str
    offer_id: str
    raw_text: str
