import asyncio
import json
import logging
import re
from operator import itemgetter
from pathlib import Path
from typing import Any

from chromadb import PersistentClient
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.vector_db.models import IndexedDocument, RetrievalPlan, LoadDataStrategy
from etl.vector_db.prompts import main_system_message_template, use_search_system_message_template
from etl.utils import read_json
from etl.vector_db.vector_saver import load_data

OPENAI_API_KEY = require_openai_api_key()

MODEL_EMBEDDINGS = "text-embedding-3-small"

FRESH_DATA_RELOAD = True  # if set to True, the data will be first deleted, then loaded, for testing purposes

CHROMA_DB_PATH = Path("chroma_langchain_db")

MAX_CONTEXT_DOCS = 5
MAX_CHROMA_BATCH = 5461
TRANSACTION_ID_PATTERN = re.compile(r"\b(?:\d{6,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b")


class LLMReturnedFaultyDataFormatError(Exception):
    """Raised when the llm returns unexpected data format"""

    pass


def to_json_log(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def document_log_payload(record: Any) -> dict[str, Any]:
    return {
        "filepath": record.filepath,
        "source_url": record.source_url,
        "title": record.title,
        "offer_id": record.offer_id,
        # "raw_text": record.raw_text,
    }


def unique_strings(values: list[str]) -> list[str]:
    """removes duplicates while preserving order. It is used to keep offer IDs from repeating"""
    return list(dict.fromkeys(values))


def extract_offer_ids_from_text(*values: str) -> tuple[str, ...]:
    """scans one or more text blobs and returns all matching offer IDs. It is used on the question, history, raw document text
    and source metadata"""
    ids: list[str] = []
    for value in values:
        ids.extend(TRANSACTION_ID_PATTERN.findall(value))
    return tuple(unique_strings(ids))


def build_indexed_document(document: Document) -> IndexedDocument:
    """converts a LangChain document into the internal"""
    
    raw_text = document.page_content.strip()
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = {}

    # extract a source path from metadata
    source = payload.get("scraper_url")
    filepath = document.metadata.get("source")
    title = document.metadata.get("title", "")
    offer_id = document.metadata.get("offer_id")

    return IndexedDocument(document=document, source_url=source, filepath=filepath, title=title, offer_id=offer_id, raw_text=raw_text)


def format_indexed_document(record: IndexedDocument) -> str:
    """converts one IndexedDocument into readable text for the prompt"""
    lines = [
        f"Title: {record.title}",
        f"Source: {record.source_url}",
    ]
    if record.offer_id:
        lines.append(f"Transaction ID: {record.offer_id}")
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
    - any offer IDs,
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
        logger.exception(f"Wrong response type from planner: {e}")
        raise
    except KeyError as e:
        logger.exception(f"The prompt formatting failed due to missing `history` or `question` variables: {e}")
        raise

    logger.debug(f"retrieval_plan_raw_output={to_json_log(payload)}")
    if (
        not isinstance(payload, dict)
        or "offer_ids" not in payload
        or "search_query" not in payload
        or "needs_search" not in payload
        or "top_k" not in payload
    ):
        logger.exception("The planner llm did not return the correct data format!")
        raise LLMReturnedFaultyDataFormatError("The planner llm did not return the correct data format!")

    # if a offer id is present, match it directly against the indexed documents
    detected_ids = extract_offer_ids_from_text(question, format_history(conversation_history, 2))
    planned_ids = payload.get("offer_ids", [])
    planned_ids = [str(item) for item in planned_ids if str(item).strip()]

    offer_ids = unique_strings(list(detected_ids) + planned_ids)
    search_query = str(payload.get("search_query", "")).strip()
    if not search_query:
        search_query = question.strip()

    needs_search = bool(payload.get("needs_search", True))
    top_k = payload.get("top_k", 3)
    if not isinstance(top_k, int) or top_k < 0:
        top_k = 3
    top_k = min(top_k, MAX_CONTEXT_DOCS)

    if offer_ids:
        needs_search = True

    logger.debug(
        "retrieval_plan_final=%s",
        to_json_log(
            {
                "needs_search": needs_search,
                "search_query": search_query,
                "offer_ids": list(offer_ids),
                "top_k": top_k,
            }
        ),
    )

    return RetrievalPlan(needs_search=needs_search, search_query=search_query, offer_ids=tuple(offer_ids), top_k=top_k)


def exact_offer_lookup(offer_ids: tuple[str, ...]) -> list[IndexedDocument]:
    if not offer_ids:
        logger.debug("exact_offer_lookup skipped: no offer ids provided")
        return []

    matched_documents: list[IndexedDocument] = []
    for offer_id in offer_ids:
        logger.debug(f"exact_offer_lookup searching for offer_id={offer_id}")
        if offer_id in indexed_documents_by_id:
            records = indexed_documents_by_id[offer_id]
            matched_documents.extend(records)
            for record in records:
                logger.debug(f"exact_offer_lookup match={to_json_log(document_log_payload(record))}")
        else:
            logger.warning(f"Did not found the exact match for offer ID: {offer_id}")

    logger.debug(
        "exact_offer_lookup_result=%s",
        to_json_log(
            {
                "offer_ids": list(offer_ids),
                "matched_count": len(matched_documents),
                "matched_sources": [record.source_url for record in matched_documents],
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
    except Exception as e:
        logger.exception(f"semantic lookup failed: {e}")
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
    exact_matches = exact_offer_lookup(plan.offer_ids)

    if not plan.needs_search and not exact_matches:
        logger.debug("hybrid_retrieve returning early with no search and no exact matches")
        return plan, []

    semantic_matches = semantic_lookup(plan.search_query, plan.top_k)

    combined: list[IndexedDocument] = []
    seen_texts: set[str] = set()

    for record in exact_matches:
        if record.raw_text not in seen_texts:
            seen_texts.add(record.raw_text)
            combined.append(record)

    added_semantic = 0
    for record in semantic_matches:
        if added_semantic >= plan.top_k:
            break
        if record.raw_text not in seen_texts:
            seen_texts.add(record.raw_text)
            combined.append(record)
            added_semantic += 1

    logger.debug(
        "hybrid_retrieve_final=%s",
        to_json_log(
            {
                "question": question,
                "combined_sources": [record.source_url for record in combined],
                "combined_documents": [document_log_payload(record) for record in combined],
            }
        ),
    )

    return plan, combined


def ask(question: str, conversation_history: list[dict[str, str]]) -> str:
    logger.info(f"received question={question}")
    logger.debug(f"current conversation_history={to_json_log(conversation_history)}")
    plan, retrieved_documents = hybrid_retrieve(question, conversation_history)

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
                    "offer_ids": list(plan.offer_ids),
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
    logger.info(f"generated answer length={len(message)}")
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
            print("An unexpected error occurred, sorry :(")
            logger.exception(f"Error while displaying output to the user: {e}")
            break


if __name__ == "__main__":
    setup_logging()
    logger = logging.getLogger("vector_db")

    llm = ChatOpenAI(model=MODEL)
    embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS)

    vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))

    documents = load_data(vector_store, LoadDataStrategy.OldDataOnly)
    print("finished loading documents, count=", len(documents))

    indexed_documents: list[IndexedDocument] = [build_indexed_document(document) for document in documents]
    
    from collections import defaultdict
    indexed_documents_by_id: dict[str, list[IndexedDocument]] = defaultdict(list)
    for record in indexed_documents:
        if record.offer_id:
            indexed_documents_by_id[record.offer_id].append(record)
            
    indexed_documents_by_filepath: dict[str, list[IndexedDocument]] = defaultdict(list)
    for record in indexed_documents:
        if record.filepath:
            indexed_documents_by_filepath[record.filepath].append(record)
    logger.info(f"built in-memory indexed_documents count={len(indexed_documents)}")

    main()
