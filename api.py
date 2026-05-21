import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from pydantic import BaseModel

from etl.llms import require_openai_api_key


class SearchRequest(BaseModel):
    query: str


class TenderDetail(BaseModel):
    offer_id: str
    title: str
    score: float
    source_url: str
    deadline: str
    buyer: str
    description: str
    full_data: str
    raw_text_preview: str


app = FastAPI(title="BidBot Search API")

OPENAI_API_KEY = require_openai_api_key()

BASE_DIR = Path(__file__).resolve().parent
CHROMA_DB_PATH = BASE_DIR / "etl" / "vector_db" / "chroma_langchain_db"
PARSED_DIR = BASE_DIR / "data" / "parsed"

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))


@app.post("/search", response_model=list[TenderDetail])
def search_tenders(request: SearchRequest):
    search_query = request.query if request.query.strip() else "przetarg"
    raw_docs = vector_store.similarity_search_with_score(search_query, k=50)

    offers = {}
    for doc, score in raw_docs:
        offer_id = doc.metadata.get("offer_id", "Nieznane ID")
        if offer_id == "Nieznane ID":
            continue

        if offer_id not in offers:
            offers[offer_id] = {"score": float(score), "title": doc.metadata.get("title", "Brak tytułu"), "text_chunks": []}
        offers[offer_id]["text_chunks"].append(doc.page_content.strip())

    results = []
    for offer_id, data in offers.items():
        payload = {}

        if PARSED_DIR.exists():
            for json_file in PARSED_DIR.rglob("*.json"):
                if offer_id in json_file.name:
                    try:
                        with open(json_file, encoding="utf-8") as f:
                            payload = json.load(f)
                        break
                    except Exception:
                        pass

        title = payload.get("title", data["title"])
        source_url = payload.get("scraper_url", "Brak linku")

        issuers = payload.get("issuers", [])
        buyer = issuers[0].get("title", "Brak danych") if issuers else "Brak danych"

        raw_deadline = payload.get("submittingOffersDeadline", "Brak danych")
        if raw_deadline != "Brak danych":
            try:
                dt = datetime.fromisoformat(raw_deadline)
                deadline = dt.strftime("%d.%m.%Y, godz. %H:%M")
            except ValueError:
                deadline = raw_deadline
        else:
            deadline = "Brak danych"

        desc_text = payload.get("description", "")
        tags = payload.get("enrichment", {}).get("tags", [])
        if tags:
            desc_text += f"\nTagi: {', '.join(tags)}"

        description = desc_text.strip() if desc_text.strip() else "Brak opisu"

        full_data_str = json.dumps(payload, ensure_ascii=False, indent=2) if payload else ""
        raw_text_combined = "\n\n--- FRAGMENT Z BAZY WEKTOROWEJ ---\n\n".join(data["text_chunks"])

        results.append(
            TenderDetail(
                offer_id=offer_id,
                title=title,
                score=data["score"],
                source_url=source_url,
                deadline=deadline,
                buyer=buyer,
                description=description,
                full_data=full_data_str,
                raw_text_preview=raw_text_combined,
            )
        )

    return results
