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
    keywords: list[str] = Field(description="A list of keywords from the user prompt for filtering tag summaries", default_factory=list)
    offer_ids: list[str] = Field(description="Any offer IDs")
    excluded_offer_ids: list[str] = Field(description="A list of offer IDs that were already presented", default_factory=list)
    top_k: int = Field(description="The desired top_k documents")
    warning: bool = Field(description="True if the user attempts jailbreaking or rule-breaking", default=False)


class OfferSummary(BaseModel):
    offer_id: str = Field(description="Transaction ID of the offer")
    title: str = Field(description="Title of the offer")
    source_url: str = Field(description="URL to the offer source")
    tags: list[str] = Field(description="Tags associated with the offer", default_factory=list)
    short_description: str = Field(description="A brief snippet of the description", default="")


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    filepath: str  # file absolute filepath to .json document
    source_url: str
    title: str
    offer_id: str
    raw_text: str
    source_type: str = "unknown"
