import json
import logging
import os
import re
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from operator import itemgetter
from pathlib import Path
from typing import Any

from chromadb import PersistentClient
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.document_loaders import JSONLoader
from langchain_community.document_loaders.directory import DirectoryLoader
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from prompts import main_system_message_template, use_search_system_message_template

DOTENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(DOTENV_PATH)


def require_openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"OPENAI_API_KEY is not set. Configure it in the environment or in {DOTENV_PATH}.")
    return api_key


OPENAI_API_KEY = require_openai_api_key()

MODEL = "gpt-4o-mini"
MODEL_EMBEDDINGS = "text-embedding-3-small"

FRESH_DATA_RELOAD = False  # if set to True, the data will be first deleted, then loaded, for testing purposes

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data"
PARSED_JSON_PATH = DATA_PATH / "parsed"
ATTACHMENTS_PATH = DATA_PATH / "attachments"

CHROMA_DB_PATH = "./chroma_langchain_db"
LOG_PATH = Path(__file__).resolve().with_name("vector_db.log")

MAX_CONTEXT_DOCS = 5
MAX_SEMANTIC_RESULTS = 4
MAX_CHROMA_BATCH = 5461
TRANSACTION_ID_PATTERN = re.compile(r"\b(?:\d{6,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b")


class LLMReturnedFaultyDataFormatError(Exception):
    """Raised when the llm returns unexpected data format"""

    pass


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


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("bidbot.vector_db")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger


def to_json_log(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def document_log_payload(record: Any) -> dict[str, Any]:
    return {
        "source": record.source,
        "title": record.title,
        "transaction_id": record.transaction_id,
        # "raw_text": record.raw_text,
    }


def unique_strings(values: list[str]) -> list[str]:
    """removes duplicates while preserving order. It is used to keep transaction IDs from repeating"""
    return list(dict.fromkeys(values))


def extract_transaction_ids_from_text(*values: str) -> tuple[str, ...]:
    """scans one or more text blobs and returns all matching transaction IDs. It is used on the question, history, raw document text
    and source metadata"""
    ids: list[str] = []
    for value in values:
        ids.extend(TRANSACTION_ID_PATTERN.findall(value))
    return tuple(unique_strings(ids))


def build_indexed_document(document: Document) -> IndexedDocument:
    """converts a LangChain document into the internal"""

    # try to parse the page content as JSON
    raw_text = document.page_content.strip()
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = {}

    # extract a source path from metadata
    source = payload.get("scraper_url")

    return IndexedDocument(document=document, source=source, title=payload["title"], transaction_id=payload["id"], raw_text=raw_text)


def format_indexed_document(record: IndexedDocument) -> str:
    """converts one IndexedDocument into readable text for the prompt"""
    lines = [
        f"Source: {record.source}",
        f"Title: {record.title}",
    ]
    if record.transaction_id:
        lines.append(f"Transaction ID: {record.transaction_id}")
    lines.append("Content:")
    lines.append(record.raw_text)
    return "\n".join(lines)


def format_history(conversation_history: list[dict[str, str]], max_turns: int = 6) -> str:
    """turns the last few chat turns into a compact transcript"""
    recent_turns = conversation_history[-max_turns:]
    if not recent_turns:
        return "No prior conversation."

    chunks = []
    for turn in recent_turns:
        chunks.append(f"User: {turn['user']}\nAssistant: {turn['assistant']}")
    return "\n\n".join(chunks)


def plan_search(question: str, conversation_history: list[dict[str, str]]) -> RetrievalPlan:
    """
    asks an LLM to produce a JSON plan containing:

    - whether retrieval is needed,
    - a focused search query,
    - any transaction IDs,
    - the desired top_k.
    """
    history_text = format_history(conversation_history)
    planning_prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessagePromptTemplate.from_template(use_search_system_message_template),
            (
                "human",
                "Conversation history:\n{history}\n\nCurrent question:\n{question}",
            ),
        ]
    )
    planner = ChatOpenAI(model=MODEL, temperature=0, max_retries=3, model_kwargs={"response_format": {"type": "json_object"}})

    try:
        response = planner.invoke(planning_prompt.format_messages(history=history_text, question=question)).content.strip()
        payload = json.loads(response)
    except (AttributeError, json.JSONDecodeError, TypeError) as e:
        logger.error(f"Wrong response type from planner: {e}")
        raise
    except KeyError as e:
        logger.error(f"The prompt formatting failed due to missing `history` or `question` variables: {e}")
        raise

    logger.debug("retrieval_plan_raw_output=%s", to_json_log(payload))
    if (
        not isinstance(payload, dict)
        or "transaction_ids" not in payload
        or "search_query" not in payload
        or "needs_search" not in payload
        or "top_k" not in payload
    ):
        logger.error("The planner llm did not return the correct data format!")
        raise LLMReturnedFaultyDataFormatError("The planner llm did not return the correct data format!")

    # if a transaction id is present, match it directly against the indexed documents
    detected_ids = extract_transaction_ids_from_text(question, format_history(conversation_history, 2))
    planned_ids = payload.get("transaction_ids", [])
    planned_ids = [str(item) for item in planned_ids if str(item).strip()]

    transaction_ids = unique_strings(list(detected_ids) + planned_ids)
    search_query = str(payload.get("search_query", "")).strip()
    if not search_query:
        search_query = question.strip()

    needs_search = bool(payload.get("needs_search", True))
    top_k = payload.get("top_k", 3)
    if not isinstance(top_k, int) or top_k < 0:
        top_k = 3
    top_k = min(top_k, MAX_CONTEXT_DOCS)

    if transaction_ids:
        needs_search = True

    logger.debug(
        "retrieval_plan_final=%s",
        to_json_log(
            {
                "needs_search": needs_search,
                "search_query": search_query,
                "transaction_ids": list(transaction_ids),
                "top_k": top_k,
            }
        ),
    )

    return RetrievalPlan(needs_search=needs_search, search_query=search_query, transaction_ids=tuple(transaction_ids), top_k=top_k)


def exact_transaction_lookup(transaction_ids: tuple[str, ...]) -> list[IndexedDocument]:
    if not transaction_ids:
        logger.debug("exact_transaction_lookup skipped: no transaction ids provided")
        return []

    matched_documents: list[IndexedDocument] = []
    for transaction_id in transaction_ids:
        logger.debug("exact_transaction_lookup searching for transaction_id=%s", transaction_id)
        if transaction_id in indexed_documents_by_id:
            record = indexed_documents_by_id[transaction_id]
            matched_documents.append(record)
            logger.debug("exact_transaction_lookup match=%s", to_json_log(document_log_payload(record)))
        else:
            logger.warning(f"Did not found the exact match for transaction ID: {transaction_id}")

    logger.debug(
        "exact_transaction_lookup_result=%s",
        to_json_log(
            {
                "transaction_ids": list(transaction_ids),
                "matched_count": len(matched_documents),
                "matched_sources": [record.source for record in matched_documents],
            }
        ),
    )
    return matched_documents


def semantic_lookup(search_query: str, limit: int) -> list[IndexedDocument]:
    if not search_query.strip() or limit <= 0:
        logger.debug(
            "semantic_lookup skipped=%s",
            to_json_log({"search_query": search_query, "limit": limit}),
        )
        return []

    logger.debug(
        "semantic_lookup_query=%s",
        to_json_log(
            {
                "search_query": search_query,
                "limit": limit,
            }
        ),
    )

    try:
        results = vector_store.similarity_search_with_relevance_scores(
            search_query,
            k=limit,
        )
    except Exception:
        logger.exception("semantic_lookup failed")
        return []

    results.sort(key=itemgetter(1), reverse=True)  # itemgetter(1) is the same as: lambda x: x[1], but faster

    logger.debug(
        "semantic_lookup_raw_results=%s",
        to_json_log(
            [
                {
                    "score": score,
                    "metadata": document.metadata,
                    "page_content": document.page_content,
                }
                for document, score in results
            ]
        ),
    )

    semantic_matches: list[IndexedDocument] = [build_indexed_document(document) for document, _ in results[:limit]]

    logger.debug(
        "semantic_lookup_selected=%s",
        to_json_log([document_log_payload(record) for record in semantic_matches]),
    )

    return semantic_matches


def hybrid_retrieve(question: str, conversation_history: list[dict[str, str]]) -> tuple[RetrievalPlan, list[IndexedDocument]]:
    plan = plan_search(question, conversation_history)
    exact_matches = exact_transaction_lookup(plan.transaction_ids)

    if not plan.needs_search and not exact_matches:
        logger.debug("hybrid_retrieve returning early with no search and no exact matches")
        return plan, []

    semantic_matches = semantic_lookup(plan.search_query, plan.top_k)

    combined: list[IndexedDocument] = []
    seen_sources: set[str] = set()
    remaining = max(plan.top_k - len(exact_matches), 0)
    for i, record in enumerate([*exact_matches, *semantic_matches]):
        if record.source in seen_sources:
            continue
        seen_sources.add(record.source)
        combined.append(record)

        if i >= remaining:
            break

    logger.debug(
        "hybrid_retrieve_final=%s",
        to_json_log(
            {
                "question": question,
                "combined_sources": [record.source for record in combined],
                "combined_documents": [document_log_payload(record) for record in combined],
            }
        ),
    )

    return plan, combined


def delete_collection(chroma_path: str, collection_name: str):
    try:
        chroma_client = PersistentClient(path=chroma_path)
        chroma_client.delete_collection(collection_name)
        logger.info("deleted collection=%s from path=%s", collection_name, chroma_path)
        print(f"Collection {collection_name} deleted successfully.")
    except Exception as e:
        logger.exception("failed to delete collection=%s from path=%s", collection_name, chroma_path)
        raise Exception(f"Unable to delete collection: {e}") from e


def add_documents_to_vector_store(documents: list[Document], vector_store: Chroma):
    for i in range(0, len(documents), MAX_CHROMA_BATCH):
        batch = documents[i : i + MAX_CHROMA_BATCH]
        vector_store.add_documents(batch)


def ask(question: str, conversation_history: list[dict[str, str]]) -> str:
    logger.info("received question=%s", question)
    logger.debug("current conversation_history=%s", to_json_log(conversation_history))
    try:
        plan, retrieved_documents = hybrid_retrieve(question, conversation_history)
    except Exception:
        raise  # propagate the error further

    if not retrieved_documents:
        context = "No relevant evidence was retrieved from the document store."
    else:
        context = "\n\n---\n\n".join(format_indexed_document(record) for record in retrieved_documents)

    logger.debug(
        "final_prompt_context=%s",
        to_json_log(
            {
                "question": question,
                "plan": {
                    "needs_search": plan.needs_search,
                    "search_query": plan.search_query,
                    "transaction_ids": list(plan.transaction_ids),
                    "top_k": plan.top_k,
                },
                "retrieved_documents": [document_log_payload(record) for record in retrieved_documents],
                "context": context,
            }
        ),
    )

    answer_prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessagePromptTemplate.from_template(main_system_message_template),
            (
                "human",
                "Conversation history:\n{history}\n\nUser question:\n{question}",
            ),
        ]
    )

    history_text = format_history(conversation_history)
    messages = answer_prompt.format_messages(history=history_text, question=question, context=context)
    message = ""
    for response in llm.stream(messages):
        message += response.content
    logger.info("generated answer length=%d", len(message))
    return message


def main():
    print("Console RAG chat ready. Type your question, or 'exit' to quit.")
    conversation_history: list[dict[str, str]] = []
    while True:
        try:
            contents = input("\nYou: ").strip()
        except EOFError, KeyboardInterrupt:
            print("\nBye.")
            break

        if not contents:
            continue
        if contents.lower() in {"exit", "quit"}:
            print("Bye.")
            break

        try:
            answer = ask(contents, conversation_history)
            print(f"Assistant: {answer}")
            conversation_history.append({"user": contents, "assistant": answer})
        except Exception as e:
            print(f"Error: {e}")
            break


if __name__ == "__main__":
    logger = configure_logger()

    llm = ChatOpenAI(model=MODEL)

    embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS)

    vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=CHROMA_DB_PATH)

    existing_data = vector_store.get()
    existing_ids = existing_data["ids"]

    documents = []

    if FRESH_DATA_RELOAD or len(existing_ids) == 0:
        loader = DirectoryLoader(
            str(PARSED_JSON_PATH), glob="**/*.json", loader_cls=JSONLoader, loader_kwargs={"jq_schema": ".", "text_content": False}
        )  # type: ignore[arg-type]

        documents = loader.load()
        logger.info("loaded documents from parsed_json_path=%s count=%d", PARSED_JSON_PATH, len(documents))

        # clear collection before adding documents
        if len(existing_ids) > 0:
            logger.info("clearing existing vector store ids count=%d", len(existing_ids))
            vector_store.delete(existing_ids)

        add_documents_to_vector_store(documents, vector_store)
        logger.info("added documents to vector store count=%d", len(documents))
    else:
        logger.info("loading documents from vector store count=%d", len(existing_ids))
        for doc, meta in zip(existing_data["documents"], existing_data["metadatas"], strict=True):
            documents.append(Document(page_content=doc, metadata=meta))

    indexed_documents: list[IndexedDocument] = [build_indexed_document(document) for document in documents]
    indexed_documents_by_id: dict[str, IndexedDocument] = {record.transaction_id: record for record in indexed_documents}
    logger.info("built in-memory indexed_documents count=%d", len(indexed_documents))

    main()
