import json
import logging
import os
import re
from operator import itemgetter
from pathlib import Path
from typing import Any
import asyncio
from collections import defaultdict
from datetime import datetime
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
from etl.scrapers.settings import BASE_DIR, PARSED_DIR

OPENAI_API_KEY = require_openai_api_key()

MODEL_EMBEDDINGS = "text-embedding-3-small"

CHROMA_DB_PATH = Path("chroma_langchain_db_")
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

    offer_ids = unique_strings(list(detected_ids) + planned_ids)
    search_query = str(payload.search_query).strip()
    if not search_query:
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
        if len(combined) >= plan.top_k:
            break
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


def _load_offer_payload(offer_id: str) -> dict[str, Any]:
    if not offer_id:
        return {}
    if not PARSED_DIR.exists():
        return {}

    for json_file in PARSED_DIR.rglob("*.json"):
        if offer_id in json_file.name:
            try:
                with open(json_file, encoding="utf-8") as f:
                    payload = json.load(f)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
    return {}


def _format_deadline(raw_deadline: str) -> str:
    if not raw_deadline or raw_deadline == "Brak danych":
        return "Brak danych"
    try:
        dt = datetime.fromisoformat(raw_deadline)
        return dt.strftime("%d.%m.%Y, godz. %H:%M")
    except ValueError:
        return raw_deadline


def ask(question: str, conversation_history: list[dict[str, str]]) -> str:
    logger.info(f"received question={question}")
    logger.debug(f"current conversation_history={to_json_log(conversation_history)}")

    history_text = format_history(conversation_history)
    search_query = question.strip() or "przetarg"
    try:
        raw_docs = vector_store.similarity_search_with_score(search_query, k=50)
    except Exception as e:
        logger.exception(f"search failed: {e}")
        return "Wystąpił błąd podczas wyszukiwania. Spróbuj ponownie później."

    offers: dict[str, dict[str, Any]] = {}
    for doc, score in raw_docs:
        offer_id = doc.metadata.get("offer_id", "")
        if not offer_id:
            continue

        record = offers.setdefault(
            offer_id,
            {
                "score": float(score),
                "title": doc.metadata.get("title", "Brak tytułu"),
                "text_chunks": [],
                "attachment_chunks": [],
            },
        )
        record["score"] = min(record["score"], float(score))

        if doc.metadata.get("source_type") == "attachment":
            record["attachment_chunks"].append(doc.page_content.strip())
        else:
            record["text_chunks"].append(doc.page_content.strip())

    if not offers:
        return "Brak dopasowanych ofert w bazie."

    results: list[dict[str, Any]] = []
    for offer_id, data in offers.items():
        payload = _load_offer_payload(offer_id)

        title = payload.get("title", data["title"])
        source_url = payload.get("scraper_url", "Brak linku")

        issuers = payload.get("issuers", []) if isinstance(payload, dict) else []
        buyer = issuers[0].get("title", "Brak danych") if issuers else "Brak danych"

        raw_deadline = payload.get("submittingOffersDeadline", "Brak danych") if isinstance(payload, dict) else "Brak danych"
        deadline = _format_deadline(raw_deadline)

        desc_text = payload.get("description", "") if isinstance(payload, dict) else ""
        tags = payload.get("enrichment", {}).get("tags", []) if isinstance(payload, dict) else []
        if tags:
            desc_text += f"\nTagi: {', '.join(tags)}"

        description = desc_text.strip() if desc_text.strip() else "Brak opisu"

        full_data_str = json.dumps(payload, ensure_ascii=False, indent=2) if payload else ""
        raw_text_combined = "\n\n--- FRAGMENT Z BAZY WEKTOROWEJ ---\n\n".join(data["text_chunks"][:6])
        attachment_preview = "\n\n--- FRAGMENT Z ZAŁĄCZNIKÓW ---\n\n".join(data["attachment_chunks"][:6])

        results.append(
            {
                "offer_id": offer_id,
                "title": title,
                "score": data["score"],
                "source_url": source_url,
                "deadline": deadline,
                "buyer": buyer,
                "description": description,
                "full_data": full_data_str,
                "raw_text_preview": raw_text_combined,
                "attachments_preview": attachment_preview,
            }
        )

    summarizer = ChatOpenAI(model=MODEL, temperature=0, max_retries=3)
    summary_prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessagePromptTemplate.from_template(
                """You are an expert assistant for analyzing public procurement tenders.
You have access to a list of tender data objects matching the following schema:
- offer_id (str): Unique identifier of the tender
- title (str): Title of the tender
- score (float): Search relevance score
- source_url (str): URL to the original tender
- deadline (str): Deadline for submitting offers
- buyer (str): The entity that published the tender
- description (str): Extracted description and tags
- full_data (str): Raw JSON string of the complete tender data
- raw_text_preview (str): Text chunks extracted from the tender documents
- attachments_preview (str): Text chunks extracted from the attachments

Use the conversation history to maintain continuity (follow-ups, references, tone).
Your goal is to summarize all the provided tender data and present it in a clear, user-friendly Markdown format.
Focus on the most important aspects: buyer, deadline, and a concise summary of the requirements. Group related tenders if possible, and structure the response so the user can easily evaluate the opportunities.
"""
            ),
            (
                "human",
                "Conversation history:\n{history}\n\nUser question:\n{question}\n\nData to summarize: {summary}",
            ),
        ]
    )

    try:
        message = summarizer.invoke(
            summary_prompt.format_messages(history=history_text, question=question, summary=results)
        ).content.strip()
    except Exception as e:
        logger.exception(f"summarization failed: {e}")
        return "Wystąpił błąd podczas generowania podsumowania. Spróbuj ponownie później."

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

    vector_store = Chroma(collection_name="bid_info", embedding_function=embeddings, persist_directory=str(CHROMA_DB_PATH))

    strategy_name = os.getenv("LOAD_DATA_STRATEGY", "OldDataOnly")
    try:
        strategy = LoadDataStrategy[strategy_name]
    except KeyError:
        strategy = LoadDataStrategy.OldDataOnly

    documents = load_data(vector_store, strategy)
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
