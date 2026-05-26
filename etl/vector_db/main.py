import json
import logging
import re
from logging import DEBUG
from operator import itemgetter
from pathlib import Path
from typing import Any
import asyncio
from collections import defaultdict
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.utils import read_json
from etl.vector_db.models import IndexedDocument, RetrievalPlan, LoadDataStrategy, OfferSummary
from etl.vector_db.prompts import main_system_message_template, use_search_system_message_template
from etl.vector_db.vector_saver import load_data
from etl.scrapers.settings import BASE_DIR

OPENAI_API_KEY = require_openai_api_key()

MODEL_EMBEDDINGS = "text-embedding-3-small"

CHROMA_DB_PATH = Path("chroma_langchain_db")
TAGS_PATH = BASE_DIR / "etl" / "ketword_tagger" / "tags.json"

try:
    _tags_data = asyncio.run(read_json(TAGS_PATH))
    TAGS_STR = ", ".join(_tags_data.get("tags", [])) if isinstance(_tags_data, dict) else ""
except Exception:
    TAGS_STR = ""

MAX_CONTEXT_DOCS = 10
MAX_CHROMA_BATCH = 5461
TRANSACTION_ID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


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
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        payload = {}

    # extract a source path from metadata
    source = payload.get("scraper_url", "no url provided")
    filepath = document.metadata.get("source", "document not downloaded")
    title = document.metadata.get("title", "unknown")
    offer_id = document.metadata.get("offer_id", "unknown")
    source_type = document.metadata.get("source_type", "unknown")

    return IndexedDocument(document=document, source_url=source, filepath=filepath, title=title, offer_id=offer_id, raw_text=raw_text, source_type=source_type)


def format_indexed_document(record: IndexedDocument, detailed: bool = False) -> str:
    """converts one IndexedDocument into readable text for the prompt"""
    if detailed:
        lines = [
            f"Title: {record.title}",
            f"Source: {record.source_url}",
        ]
        if record.offer_id:
            lines.append(f"Offer ID: {record.offer_id}")
        lines.append("Content:")
        lines.append(record.raw_text)
        return "\n".join(lines)

    tags = []
    desc = ""
    if record.source_type == "json":
        try:
            payload = json.loads(record.raw_text)
            enrichment = payload.get("enrichment", {})
            if isinstance(enrichment, dict):
                tags = enrichment.get("tags", [])
            desc = str(payload.get("description", ""))[:500] + "..."
        except Exception:
            pass

    summary = OfferSummary(
        offer_id=record.offer_id,
        title=record.title,
        source_url=record.source_url,
        tags=tags,
        short_description=desc,
    )
    return summary.model_dump_json(indent=2)


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
            SystemMessagePromptTemplate.from_template(use_search_system_message_template, partial_variables={"tags": TAGS_STR}),
            (
                "human",
                "Conversation history:\n{history}\n\nCurrent question:\n{question}",
            ),
        ]
    )
    planner = ChatOpenAI(model=MODEL, temperature=0, max_retries=3).with_structured_output(RetrievalPlan)

    try:
        payload = planner.invoke(planning_prompt.format_messages(history=history_text, question=question))
    except KeyError as e:
        logger.exception(f"The prompt formatting failed due to missing `history` or `question` variables: {e}")
        raise
    except Exception as e:
        logger.exception(f"Error from planner: {e}")
        raise

    logger.debug(f"retrieval_plan_raw_output={payload}")

    # if a offer id is present, match it directly against the indexed documents
    detected_ids = extract_offer_ids_from_text(question, format_history(conversation_history, 2))
    planned_ids = payload.offer_ids
    planned_ids = [str(item) for item in planned_ids if str(item).strip()]

    # intersect planner logic with extracted regex to limit hallucinations
    detected_ids_set = set(detected_ids)
    offer_ids = [pid for pid in planned_ids if pid in detected_ids_set]
    offer_ids = unique_strings(offer_ids)
    
    search_query = str(payload.search_query).strip()
    if not search_query and not offer_ids:
        search_query = question.strip()

    needs_search = bool(payload.needs_search)
    top_k = payload.top_k
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

    return RetrievalPlan(needs_search=needs_search, search_query=search_query, offer_ids=offer_ids, top_k=top_k)


def exact_offer_lookup(offer_ids: list[str]) -> list[IndexedDocument]:
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


def semantic_lookup(plan: RetrievalPlan) -> list[IndexedDocument]:
    search_query = plan.search_query.strip()
    if not search_query or plan.top_k <= 0:
        logger.debug(
            "semantic_lookup skipped=%s",
            to_json_log({"search_query": search_query, "limit": plan.top_k}),
        )
        return []

    logger.debug(
        "semantic_lookup_query=%s",
        to_json_log(
            {
                "search_query": search_query,
                "limit": plan.top_k,
            }
        ),
    )

    where_filter = {}
    if not plan.offer_ids:
        where_filter["source_type"] = "json"

    if plan.excluded_offer_ids:
        if len(plan.excluded_offer_ids) == 1:
            where_filter["offer_id"] = {"$ne": plan.excluded_offer_ids[0]}
        else:
            where_filter["offer_id"] = {"$nin": list(plan.excluded_offer_ids)}

    chroma_filter = None
    if len(where_filter) > 1:
        chroma_filter = {"$and": [{k: v} for k, v in where_filter.items()]}
    elif len(where_filter) == 1:
        chroma_filter = where_filter

    try:
        kwargs: dict[str, Any] = {"k": plan.top_k}
        if chroma_filter:
            kwargs["filter"] = chroma_filter

        results = vector_store.similarity_search_with_relevance_scores(
            search_query,
            **kwargs
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

    semantic_matches: list[IndexedDocument] = [build_indexed_document(document) for document, _ in results[:plan.top_k]]

    logger.debug(
        "semantic_lookup_selected=%s",
        to_json_log([document_log_payload(record) for record in semantic_matches]),
    )

    return semantic_matches


def hybrid_retrieve(question: str, conversation_history: list[dict[str, str]]) -> tuple[RetrievalPlan, list[IndexedDocument], bool]:
    plan = plan_search(question, conversation_history)
    detailed = len(plan.offer_ids) > 0 # czemu tak?
    exact_matches = exact_offer_lookup(plan.offer_ids)

    if not plan.needs_search and not exact_matches:
        logger.debug("hybrid_retrieve returning early with no search and no exact matches")
        return plan, [], detailed

    semantic_matches = semantic_lookup(plan)

    combined: list[IndexedDocument] = []
    seen_texts: set[str] = set()

    for record in exact_matches:
        if record.raw_text not in seen_texts:
            seen_texts.add(record.raw_text)
            combined.append(record)

    for record in semantic_matches:
        if len(combined) >= plan.top_k:
            break
        if record.raw_text not in seen_texts:
            seen_texts.add(record.raw_text)
            combined.append(record)

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

    return plan, combined, detailed


def ask(question: str, conversation_history: list[dict[str, str]]) -> str:
    logger.debug(f"received question={question}")
    logger.debug(f"current conversation_history={to_json_log(conversation_history)}")
    plan, retrieved_documents, detailed = hybrid_retrieve(question, conversation_history)

    if plan.warning:
        return "Przepraszam, ale to wykracza poza moje instrukcje. Mogę za to opowiedzieć Ci o najnowszych ofertach publicznych!"

    if not retrieved_documents:
        context = "No relevant evidence was retrieved from the document store."
    else:
        context = "\n\n---\n\n".join(format_indexed_document(record, detailed=detailed) for record in retrieved_documents)

    logger.debug(
        "final_prompt_context=%s",
        to_json_log(
            {
                "question": question,
                "plan": {
                    "needs_search": plan.needs_search,
                    "search_query": plan.search_query,
                    "offer_ids": list(plan.offer_ids),
                    "excluded_offer_ids": list(plan.excluded_offer_ids),
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
    logger.debug(f"generated answer length={len(message)}")
    logger.debug(f"generated answer={message}")
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

    vector_store = Chroma(collection_name="bid_info", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))

    documents = load_data(vector_store, LoadDataStrategy.OldDataOnly)
    print("finished loading documents, count=", len(documents))

    indexed_documents: list[IndexedDocument] = [build_indexed_document(document) for document in documents]

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
