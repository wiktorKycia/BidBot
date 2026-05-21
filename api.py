import logging
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel

import etl.vector_db.main as ragemain
from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.vector_db.main import ask, build_indexed_document
from etl.vector_db.models import LoadDataStrategy
from etl.vector_db.vector_saver import load_data


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]]


class ChatResponse(BaseModel):
    answer: str


app = FastAPI(title="BidBot Chat API")

OPENAI_API_KEY = require_openai_api_key()

BASE_DIR = Path(__file__).resolve().parent
CHROMA_DB_PATH = BASE_DIR / "etl" / "vector_db" / "chroma_langchain_db"

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))


try:
    setup_logging()
except Exception:
    pass

ragemain.logger = logging.getLogger("vector_db")
ragemain.vector_store = vector_store
ragemain.llm = ChatOpenAI(model=MODEL)

documents = load_data(vector_store, LoadDataStrategy.OldDataOnly)
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
@app.post("/chat", response_model=ChatResponse)
def chat_with_bot(request: ChatRequest):
    try:
        bot_answer = ask(request.message, request.history)
        return ChatResponse(answer=bot_answer)
    except Exception as e:
        ragemain.logger.exception(f"Krytyczny błąd podczas wywołania czatu: {e}")
        raise HTTPException(status_code=500, detail=str(e))