from pathlib import Path

from fastapi import FastAPI, HTTPException
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel

from etl.llms import require_openai_api_key


class SearchRequest(BaseModel):
    query: str
    category: str = "Dowolna"
    min_score: float = 0.2
    source_platform: str = "Wszystkie"


class SearchResult(BaseModel):
    offer_id: str
    title: str
    score: float
    source_type: str
    content_preview: str
    raw_source_path: str
    original_platform: str


app = FastAPI(title="BidBot API", version="1.0")

OPENAI_API_KEY = require_openai_api_key()
CHROMA_DB_PATH = Path("chroma_langchain_db")
MODEL_EMBEDDINGS = "text-embedding-3-small"

embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS)
vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))


@app.post("/search", response_model=list[SearchResult])
def search_bids(request: SearchRequest):
    final_query = request.query
    if request.category != "Dowolna":
        final_query = f"{request.category} {request.query}".strip()

    if not final_query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    raw_results = vector_store.similarity_search_with_relevance_scores(final_query, k=20)

    filtered_results = []
    for doc, score in raw_results:
        meta = doc.metadata
        doc_platform = meta.get("original_platform", "Nieznane")

        if score >= request.min_score and (request.source_platform == "Wszystkie" or request.source_platform == doc_platform):
            filtered_results.append(
                SearchResult(
                    offer_id=meta.get("offer_id", "Brak ID"),
                    title=meta.get("title", "Brak tytułu"),
                    score=score,
                    source_type=meta.get("source_type", "N/A"),
                    content_preview=doc.page_content[:1000],
                    raw_source_path=meta.get("source", ""),
                    original_platform=doc_platform,
                )
            )

    return filtered_results
