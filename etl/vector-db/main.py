import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chromadb import PersistentClient
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.document_loaders import DirectoryLoader, JSONLoader
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

load_dotenv("../../.env")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

MODEL = "gpt-4o-mini"
MODEL_EMBEDDINGS = "text-embedding-3-small"


CHROMA_DB_PATH = "./chroma_langchain_db"
embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS)

vector_store = Chroma(collection_name="bid_info_json", embedding_function=embeddings, persist_directory=CHROMA_DB_PATH)

MAX_CONTEXT_DOCS = 5
MAX_SEMANTIC_RESULTS = 4
TRANSACTION_ID_PATTERN = re.compile(r"\b\d{6,}\b")


@dataclass(frozen=True)
class RetrievalPlan:
    needs_search: bool
    search_query: str
    transaction_ids: tuple[str, ...]
    top_k: int


@dataclass(frozen=True)
class IndexedDocument:
    document: Any
    source: str
    title: str
    transaction_ids: tuple[str, ...]
    raw_text: str


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def extract_transaction_ids_from_text(*values: str) -> tuple[str, ...]:
    ids: list[str] = []
    for value in values:
        ids.extend(TRANSACTION_ID_PATTERN.findall(value))
    return tuple(unique_strings(ids))


def find_first_key_value(payload: Any, candidate_keys: tuple[str, ...]) -> str:
    if isinstance(payload, dict):
        for key in candidate_keys:
            if key in payload and payload[key] not in (None, "", "N/A"):
                return str(payload[key])
        for value in payload.values():
            found = find_first_key_value(value, candidate_keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_first_key_value(item, candidate_keys)
            if found:
                return found
    return ""


def build_indexed_document(document: Any) -> IndexedDocument:
    raw_text = document.page_content.strip()
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        payload = {}

    source = (
        document.metadata.get("source")
        or document.metadata.get("location")
        or document.metadata.get("path")
        or document.metadata.get("file_path")
        or "Unknown source"
    )
    title = find_first_key_value(payload, ("transaction_title", "event_title", "title", "name", "subject", "offer_title", "procedure_name"))
    if not title:
        title = raw_text[:120].replace("\n", " ")

    id_candidates = extract_transaction_ids_from_text(raw_text, json.dumps(payload, ensure_ascii=False), source)
    return IndexedDocument(document=document, source=source, title=title, transaction_ids=id_candidates, raw_text=raw_text)


def format_indexed_document(record: IndexedDocument) -> str:
    lines = [
        f"Source: {record.source}",
        f"Title: {record.title}",
    ]
    if record.transaction_ids:
        lines.append(f"Transaction IDs: {', '.join(record.transaction_ids)}")
    lines.append("Content:")
    lines.append(record.raw_text)
    return "\n".join(lines)


def format_history(conversation_history: list[dict[str, str]], max_turns: int = 6) -> str:
    recent_turns = conversation_history[-max_turns:]
    if not recent_turns:
        return "No prior conversation."

    chunks = []
    for turn in recent_turns:
        chunks.append(f"User: {turn['user']}\nAssistant: {turn['assistant']}")
    return "\n\n".join(chunks)


def plan_search(question: str, conversation_history: list[dict[str, str]]) -> RetrievalPlan:
    history_text = format_history(conversation_history)
    planning_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You create a retrieval plan for a RAG assistant about public procurement offers.
Return only valid JSON with these keys:
{"needs_search": true/false, "search_query": "...", "transaction_ids": ["..."], "top_k": 3}

Rules:
- Prefer exact transaction IDs if the user mentions them explicitly.
- If the question refers to earlier turns, use the conversation history to infer the likely topic.
- Make search_query short and focused on the offer title, organization, or subject.
- If the question is unrelated or only small talk, set needs_search to false, search_query to an empty string, transaction_ids to an empty list, and top_k to 0.
- top_k should usually be between 1 and 5.
""",
            ),
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
    except Exception:
        payload = {}

    detected_ids = extract_transaction_ids_from_text(question, history_text)
    planned_ids = payload.get("transaction_ids", []) if isinstance(payload, dict) else []
    if isinstance(planned_ids, list):
        planned_ids = [str(item) for item in planned_ids if str(item).strip()]
    else:
        planned_ids = []

    transaction_ids = unique_strings(list(detected_ids) + planned_ids)
    search_query = str(payload.get("search_query", "")).strip() if isinstance(payload, dict) else ""
    if not search_query:
        search_query = question.strip()

    needs_search = bool(payload.get("needs_search", True)) if isinstance(payload, dict) else True
    top_k = payload.get("top_k", 3) if isinstance(payload, dict) else 3
    if not isinstance(top_k, int) or top_k < 0:
        top_k = 3
    top_k = min(top_k, MAX_CONTEXT_DOCS)

    if transaction_ids:
        needs_search = True
        top_k = max(top_k, min(3, MAX_CONTEXT_DOCS))

    return RetrievalPlan(needs_search=needs_search, search_query=search_query, transaction_ids=tuple(transaction_ids), top_k=top_k)


def exact_transaction_lookup(transaction_ids: tuple[str, ...]) -> list[IndexedDocument]:
    if not transaction_ids:
        return []

    matched_sources: set[str] = set()
    matched_documents: list[IndexedDocument] = []
    for transaction_id in transaction_ids:
        for record in indexed_documents:
            if transaction_id in record.transaction_ids or transaction_id in record.raw_text:
                if record.source not in matched_sources:
                    matched_sources.add(record.source)
                    matched_documents.append(record)
    return matched_documents


def semantic_lookup(search_query: str, excluded_sources: set[str], limit: int) -> list[IndexedDocument]:
    if not search_query.strip() or limit <= 0:
        return []

    try:
        results = vector_store.similarity_search_with_relevance_scores(
            search_query,
            k=min(limit + len(excluded_sources), MAX_SEMANTIC_RESULTS + len(excluded_sources), max(1, len(indexed_documents))),
        )
    except Exception:
        return []

    semantic_matches: list[IndexedDocument] = []
    seen_sources: set[str] = set(excluded_sources)
    for document, _score in results:
        source = (
            document.metadata.get("source")
            or document.metadata.get("location")
            or document.metadata.get("path")
            or document.metadata.get("file_path")
            or "Unknown source"
        )
        if source in seen_sources:
            continue
        seen_sources.add(source)
        indexed_record = indexed_documents_by_source.get(source)
        if indexed_record is not None:
            semantic_matches.append(indexed_record)
        else:
            raw_text = document.page_content.strip()
            title = raw_text[:120].replace("\n", " ")
            ids = extract_transaction_ids_from_text(raw_text, source)
            semantic_matches.append(IndexedDocument(document=document, source=source, title=title, transaction_ids=ids, raw_text=raw_text))

        if len(semantic_matches) >= limit:
            break

    return semantic_matches


def hybrid_retrieve(question: str, conversation_history: list[dict[str, str]]) -> tuple[RetrievalPlan, list[IndexedDocument]]:
    plan = plan_search(question, conversation_history)
    exact_matches = exact_transaction_lookup(plan.transaction_ids)

    if not plan.needs_search and not exact_matches:
        return plan, []

    semantic_limit = max(0, min(MAX_CONTEXT_DOCS, plan.top_k))
    if exact_matches:
        semantic_limit = max(0, semantic_limit - len(exact_matches))

    semantic_matches = semantic_lookup(plan.search_query, {record.source for record in exact_matches}, semantic_limit)

    combined: list[IndexedDocument] = []
    seen_sources: set[str] = set()
    for record in [*exact_matches, *semantic_matches]:
        if record.source in seen_sources:
            continue
        seen_sources.add(record.source)
        combined.append(record)
        if len(combined) >= MAX_CONTEXT_DOCS:
            break

    return plan, combined


llm = ChatOpenAI(model=MODEL)
answer_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a helpful assistant focused on procurement offers.
Answer only with information explicitly supported by the retrieved evidence.
If the evidence does not explicitly confirm a transaction, ID, or detail, say that you cannot confirm it.
Do not invent transaction numbers, titles, or organizations.
Be concise and clear, but include the exact transaction IDs when they are present in the evidence.
""",
        ),
        (
            "human",
            "Conversation history:\n{history}\n\nUser question:\n{question}\n\nRetrieved evidence:\n{context}",
        ),
    ]
)


def delete_collection(chroma_path: str, collection_name: str):
    try:
        chroma_client = PersistentClient(path=chroma_path)
        chroma_client.delete_collection(collection_name)
        print(f"Collection {collection_name} deleted successfully.")
    except Exception as e:
        raise Exception(f"Unable to delete collection: {e}") from e

data_path = Path(__file__).resolve().parent.parent / "data"
parsed_json_path = data_path / "parsed"
attachments_path = data_path / "attachments"

loader = DirectoryLoader(str(parsed_json_path), glob="**/*.json", loader_cls=JSONLoader, loader_kwargs={"jq_schema": ".", "text_content": False})  # type: ignore[arg-type]

documents = loader.load()

# clear collection before adding documents
ids = vector_store.get()["ids"]
if len(ids) > 0:
    vector_store.delete(ids)

vector_store.add_documents(documents)

indexed_documents = [build_indexed_document(document) for document in documents]
indexed_documents_by_source = {record.source: record for record in indexed_documents}


def ask(question: str, conversation_history: list[dict[str, str]]) -> str:
    plan, retrieved_documents = hybrid_retrieve(question, conversation_history)

    if not retrieved_documents:
        context = "No relevant evidence was retrieved from the document store."
    else:
        context = "\n\n---\n\n".join(format_indexed_document(record) for record in retrieved_documents)

    history_text = format_history(conversation_history)
    messages = answer_prompt.format_messages(history=history_text, question=question, context=context)
    message = ""
    for response in llm.stream(messages):
        message += response.content
    return message


def main():
    print("Console RAG chat ready. Type your question, or 'exit' to quit.")
    conversation_history: list[dict[str, str]] = []
    while True:
        try:
            contents = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
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


if __name__ == "__main__":
    main()
