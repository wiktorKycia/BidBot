import asyncio
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel

import etl.vector_db.main as ragemain
from etl.llms import MODEL
from etl.loggers import setup_logging
from etl.vector_db.main import ask, build_indexed_document
from etl.vector_db.models import LoadDataStrategy
from etl.vector_db.vector_saver import load_data


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]]


class ChatResponse(BaseModel):
    answer: str


app_ready = False

BASE_DIR = Path(__file__).resolve().parent
CHROMA_DB_PATH = BASE_DIR / "etl" / "vector_db" / "chroma_langchain_db"

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))


try:
    setup_logging()
except Exception:
    logging.getLogger(__name__).exception("Failed to initialize logging via setup_logging()")

ragemain.logger = logging.getLogger("vector_db")
ragemain.vector_store = vector_store
ragemain.llm = ChatOpenAI(model=MODEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global app_ready
    try:
        strategy_name = os.getenv("LOAD_DATA_STRATEGY", "OldDataOnly")
        try:
            strategy = LoadDataStrategy[strategy_name]
        except KeyError:
            strategy = LoadDataStrategy.OldDataOnly

        documents = await asyncio.to_thread(load_data, vector_store, strategy)
        indexed_documents = [build_indexed_document(doc) for doc in documents]

        indexed_documents_by_id = defaultdict(list)
        indexed_documents_by_filepath = defaultdict(list)

        for record in indexed_documents:
            if record.offer_id:
                indexed_documents_by_id[record.offer_id].append(record)
            if record.filepath:
                indexed_documents_by_filepath[record.filepath].append(record)
        ragemain.indexed_documents_by_id = indexed_documents_by_id
        ragemain.indexed_documents_by_filepath = indexed_documents_by_filepath
        ragemain.logger.info("Inicjalizacja zakończona sukcesem.")
        app_ready = True
    except Exception as e:
        ragemain.logger.exception(f"Błąd inicjalizacji: {e}")
    yield


app = FastAPI(title="BidBot Chat API", lifespan=lifespan)


@app.post("/chat", response_model=ChatResponse)
def chat_with_bot(request: ChatRequest):
    if not app_ready:
        raise HTTPException(status_code=503, detail="initializing")
    try:
        bot_answer = ask(request.message, request.history)
        return ChatResponse(answer=bot_answer)
    except Exception as e:
        ragemain.logger.exception(f"Krytyczny błąd podczas wywołania czatu: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/health")
def health():
    if not app_ready:
        raise HTTPException(status_code=503, detail="initializing")
    return {"status": "ready"}
